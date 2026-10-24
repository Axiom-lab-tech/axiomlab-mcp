"""Model Context Protocol (MCP) server for Axiom Lab: the tools an AI assistant uses to plan, start, follow and download films.

One tool implementation serves two transports:

* stdio  - `azimo mcp` / `python -m vidgen_sdk.mcp`: newline-delimited JSON-RPC 2.0 on stdin/stdout, calling https://axiomlab.online
           (or AZIMO_URL) with the user's API key through the regular `Vidgen` client.
* HTTP   - `vidgen/service/mcp_http.py` (POST /mcp, Streamable HTTP, stateless) builds a `Context` whose `api` calls the app's own REST
           API in-process, so authentication, validation, idempotency and the hiding of prices are the REST API's, unchanged.

The protocol is small, so it is implemented here directly (no `mcp` package). Depends only on the standard library and `vidgen_sdk`.
Nothing but protocol messages is ever written to stdout; diagnostics go to stderr. Prices are never shown: every payload is passed
through `strip_prices` before it leaves this module, and there is deliberately no price or quote tool.
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import TERMINAL_STATUSES, Vidgen, VidgenError, __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "azimo"
MAX_WAIT_SECONDS = 110
POLL_SECONDS = 5.0
MAX_INLINE_UPLOAD_BYTES = 20 * 1024 * 1024  # content_base64 uploads (the only kind a remote server accepts)
MAX_PATH_UPLOAD_BYTES = 60 * 1024 * 1024  # local files: the API's own limit
NO_KEY_MESSAGE = "No Axiom Lab API key is configured. Set AZIMO_API_KEY in the MCP server's environment or run `azimo login`."

WORKFLOWS = ("auto", "explainer", "short_drama", "creative_video", "motion_explainer", "profile_card", "product_pitch")
VIDEO_TYPES = ("project", "product", "personal", "corporate", "name_card")
RATIOS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}
JOB_STATUSES = ("queued", "running", "needs_input", "complete", "rejected", "failed", "cancelled")
WORKFLOW_BLURBS = {
    "explainer": "Live-action style film with narration and presenter: promo, product, person or company introduction.",
    "motion_explainer": "Designed-motion explainer animation for mechanisms and concepts.",
    "product_pitch": "Investor / channel pitch built on the customer's own product pictures.",
    "profile_card": "Interview-style personal card, vertical.",
    "short_drama": "Story-driven short drama advertisement.",
    "creative_video": "Open-ended creative video from a prompt and references.",
}

# Keys that carry money the customer never sees. `_usd` catches every provider-side figure (quote_usd, spent_usd, ...); wallet
# balances are the customer's own money and are kept (balance_cents, held_cents, available_cents).
_PRICE_KEY = re.compile(r"cost|price|quote|margin|estimate|(?:^|_)usd$|spent|ceiling", re.IGNORECASE)
_KEY_LIKE = re.compile(r"vg[a-z]?_[A-Za-z0-9_\-]{6,}")


def strip_prices(value: Any) -> Any:
    """A copy of `value` without any dict key that names a cost, price, quote, margin or estimate, at any depth."""
    if isinstance(value, dict):
        return {k: strip_prices(v) for k, v in value.items() if not (isinstance(k, str) and _PRICE_KEY.search(k))}
    if isinstance(value, list):
        return [strip_prices(v) for v in value]
    return value


class ToolError(Exception):
    """A failure the model should see and can act on (returned as an `isError` tool result, never as a protocol error)."""

    def __init__(self, message: str, data: Optional[dict] = None):
        super().__init__(message)
        self.data = data


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class Context:
    """What a tool needs: an API client (`Vidgen`-compatible), the transport's capabilities and an interruptible clock."""

    def __init__(self, api: Any = None, *, allow_paths: bool = True, sleep: Optional[Callable[[float], Any]] = None,
                 now: Optional[Callable[[], float]] = None, stop: Optional[threading.Event] = None, api_error: Optional[str] = None):
        self.api, self.allow_paths = api, allow_paths
        self.stop = stop or threading.Event()
        self.sleep = sleep or self.stop.wait  # sleeping ends as soon as the server is shutting down
        self.now = now or time.monotonic
        self.api_error = api_error or NO_KEY_MESSAGE

    def client(self) -> Any:
        if self.api is None:
            raise ToolError(self.api_error)
        return self.api


# -- JSON Schema subset used to validate tool arguments -----------------------------------------------------------------------------------
def validate(schema: dict, value: Any, path: str = "arguments") -> Optional[str]:
    """First problem found, or None. Covers the keywords the tool schemas use; unknown keywords are ignored."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return f"{path} must be an object"
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                return f"{path}.{name} is required"
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                problem = validate(properties[name], item, f"{path}.{name}")
            elif extra is False:
                return f"{path}.{name} is not a known argument"
            elif isinstance(extra, dict):
                problem = validate(extra, item, f"{path}.{name}")
            else:
                problem = None
            if problem:
                return problem
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            return f"{path} accepts at most {schema['maxProperties']} entries"
        return None
    if kind == "array":
        if not isinstance(value, list):
            return f"{path} must be an array"
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            return f"{path} must have between {schema.get('minItems', 0)} and {schema.get('maxItems', 'any number of')} items"
        for index, item in enumerate(value):
            problem = validate(schema.get("items", {}), item, f"{path}[{index}]")
            if problem:
                return problem
        return None
    if kind == "string":
        if not isinstance(value, str):
            return f"{path} must be a string"
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            return f"{path} must be {schema.get('minLength', 0)}-{schema.get('maxLength', 'unbounded')} characters long"
        if "pattern" in schema and not re.search(schema["pattern"], value):
            return f"{path} has an invalid format"
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{path} must be an integer"
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{path} must be a number"
    elif kind == "boolean":
        if not isinstance(value, bool):
            return f"{path} must be true or false"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of: {', '.join(str(v) for v in schema['enum'])}"
    if "minimum" in schema and value < schema["minimum"]:
        return f"{path} must be at least {schema['minimum']}"
    if "maximum" in schema and value > schema["maximum"]:
        return f"{path} must be at most {schema['maximum']}"
    return None


# -- presentation helpers -----------------------------------------------------------------------------------------------------------------
def _when(timestamp: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(timestamp)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "-"


def _money(cents: Any, currency: str = "HKD") -> str:
    """Wallet amounts are integer cents of the wallet currency (HKD): 12345 -> "HK$123.45"."""
    symbol = {"HKD": "HK$", "USD": "US$"}.get(currency)
    return f"{symbol}{(cents or 0) / 100:,.2f}" if symbol else f"{(cents or 0) / 100:,.2f} {currency}"


def _ratio_of(options: dict) -> Optional[str]:
    size = (options.get("width"), options.get("height"))
    for label, dims in RATIOS.items():
        if size == dims:
            return label
    return f"{size[0]}x{size[1]}" if all(size) else None


def _redact(text: str, api_key: Optional[str]) -> str:
    if api_key and len(api_key) >= 8:  # a stub key such as "k" would corrupt every message that contains the letter
        text = text.replace(api_key, "***")
    return _KEY_LIKE.sub("vg_***", text)


def describe_api_error(exc: VidgenError) -> str:
    if exc.status == 401:
        return "unauthenticated: the Axiom Lab API key was rejected. Check AZIMO_API_KEY (or the Authorization header), or run `azimo login` with a valid key."
    parts = [f"{exc.code}: {exc.message}".rstrip(": ")]
    if exc.status == 403 and "read-only" in exc.message.lower():
        parts.append("This API key has the viewer role and can only read; use an editor or owner key to create or change things.")
    if exc.status == 429 and exc.retry_after:
        parts.append(f"Retry in about {int(exc.retry_after)} seconds.")
    details = exc.details
    if isinstance(details, list):
        notes = [str(d.get("message") or d.get("msg")) for d in details[:5] if isinstance(d, dict) and (d.get("message") or d.get("msg"))]
        if notes and exc.message not in notes:
            parts.append("; ".join(notes))
    return " ".join(parts)


def public_workflows(raw: Any) -> List[dict]:
    rows = []
    for name, spec in ((raw or {}).get("workflows") or {}).items():
        spec = spec or {}
        row = {"name": name, "configured": bool(spec.get("configured")), "description": WORKFLOW_BLURBS.get(name, "")}
        for source, target in (("duration_seconds", "duration_seconds"), ("aspect_ratios", "aspect_ratios"), ("reference_media", "reference_media")):
            if source in spec:
                row[target] = spec[source]
        rows.append(row)
    return rows


def public_video_types(raw: Any) -> List[dict]:
    rows = raw.get("data") if isinstance(raw, dict) else raw
    result = []
    for entry in rows or []:
        result.append({"id": entry.get("id"), "name": entry.get("name"), "seconds": entry.get("seconds"),
                       "sections": [{k: s.get(k) for k in ("id", "name", "purpose")} for s in entry.get("sections") or []],
                       "inputs": [{k: i.get(k) for k in ("id", "name", "hint")} for i in entry.get("inputs") or []]})
    return result


def _input_required(required: Any) -> Optional[dict]:
    """What a waiting video asks for, without provider-side amounts (a budget stop names no figures)."""
    if not isinstance(required, dict) or not required:
        return None
    code = str(required.get("code") or "input_required")
    if code == "budget_approval":
        if required.get("approvable") is False:  # prepaid (flat price): an internal review by Axiom Lab, nothing for the user to approve or pay
            return {"code": code, "actor": "operator", "approvable": False,
                    "message": "This production is paused for a review by the Axiom Lab team. Its price does not change and the user does not need to do "
                               "anything; it continues once the review is done. Do not call azimo_approve_budget."}
        return {"code": code, "actor": "customer", "approvable": True,
                "message": "This production reached its spending limit and needs the customer's approval to continue. Ask the user, then use azimo_approve_budget."}
    waiting = {"code": code, "actor": required.get("actor"), "message": str(required.get("message") or required.get("reason") or "")[:600]}
    if code == "confirm_duration":  # the numbers the user chooses the film length from
        waiting.update({k: required[k] for k in ("suggested_duration_sec", "min_duration_sec", "max_duration_sec", "expires_at") if k in required})
    return waiting


def public_job(job: Any) -> dict:
    job = strip_prices(job or {})
    out = {k: job.get(k) for k in ("id", "status", "stage", "progress", "error", "created_at", "updated_at", "started_at", "finished_at", "requested_workflow", "parent_id") if k in job}
    result = job.get("result")
    if isinstance(result, dict):
        kept = {k: result[k] for k in ("files", "duration", "shot_count", "workflow") if result.get(k) is not None}
        waiting = _input_required(result.get("input_required"))
        if waiting:
            kept["input_required"] = waiting
        if kept:
            out["result"] = kept
    return out


def public_plan(plan: Any) -> dict:
    plan = plan or {}
    data = strip_prices(plan.get("data") or {})
    request = data.get("request") or {}
    options = request.get("options") or {}
    kept = {k: options[k] for k in ("workflow", "video_type", "duration_max", "width", "height", "subtitles", "music") if k in options}
    routing = data.get("routing") or {}
    requirements = [{"code": r.get("code"), "message": r.get("message")} for r in data.get("requirements") or [] if isinstance(r, dict)]
    return {"id": plan.get("id"), "status": data.get("status"), "reason": data.get("reason") or routing.get("reason") or None,
            "request": {"options": kept, "assets": list(request.get("assets") or [])},
            "routing": {"workflow": routing.get("workflow")}, "requirements": requirements}


def _job_text(job: dict) -> str:
    status = job.get("status", "unknown")
    head = f"Video `{job.get('id')}` is {status}"
    if job.get("stage") and job["stage"] != status:
        head += f" (stage: {job['stage']})"
    if job.get("progress") is not None and status in ("queued", "running"):
        head += f", {job['progress']}% done"
    lines = [head + "."]
    if job.get("requested_workflow"):
        lines.append(f"Workflow: {job['requested_workflow']}. Created: {_when(job.get('created_at'))}.")
    result = job.get("result") or {}
    if job.get("error"):
        lines.append(f"Error: {job['error']}")
    if result.get("duration"):
        lines.append(f"Length: {float(result['duration']):.1f} s" + (f", {result['shot_count']} shots" if result.get("shot_count") else "") + ".")
    if result.get("files"):
        lines.append("Download links (signed, they expire; fetch new ones with azimo_get_video):")
        lines.extend(f"- {name}: {url}" for name, url in result["files"].items())
    waiting = result.get("input_required")
    if waiting:
        lines.append(f"Waiting for input ({waiting['code']}): {waiting['message']}".rstrip())
    if status in ("queued", "running"):
        lines.append("Call azimo_wait_video to follow it; production usually takes several minutes.")
    return "\n".join(lines)


# -- tools --------------------------------------------------------------------------------------------------------------------------------
Result = Tuple[str, dict]


class Tool:
    def __init__(self, name: str, title: str, description: str, schema: Any, annotations: dict, run: Callable[[Context, dict], Result]):
        self.name, self.title, self.description, self._schema, self.annotations, self.run = name, title, description, schema, annotations, run

    def schema(self, allow_paths: bool = True) -> dict:
        return self._schema(allow_paths) if callable(self._schema) else self._schema

    def definition(self, allow_paths: bool = True) -> dict:
        return {"name": self.name, "title": self.title, "description": self.description, "inputSchema": self.schema(allow_paths), "annotations": dict(self.annotations)}


READ_ONLY = {"readOnlyHint": True, "openWorldHint": True}


def _writes(destructive: bool = False, idempotent: bool = False) -> dict:
    return {"readOnlyHint": False, "destructiveHint": destructive, "idempotentHint": idempotent, "openWorldHint": True}


def _object(properties: dict, required: Optional[list] = None) -> dict:
    schema: Dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


_VIDEO_ID = {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[A-Za-z0-9_-]+$", "description": "The video (job) id returned by azimo_create_video."}


def _list_workflows(ctx: Context, args: dict) -> Result:
    rows = public_workflows(ctx.client().workflows())
    lines = ["Axiom Lab workflows (ready = configured on this server):"]
    for row in rows:
        detail = row["description"]
        if row.get("duration_seconds"):
            detail += f" Duration {row['duration_seconds'][0]}-{row['duration_seconds'][1]} s."
        if row.get("aspect_ratios"):
            detail += " Ratios: " + ", ".join(row["aspect_ratios"]) + "."
        lines.append(f"- {row['name']}: {'ready' if row['configured'] else 'not configured'}. {detail}".rstrip())
    lines.append("Pass workflow to azimo_create_plan, or omit it for the default (explainer); use \"auto\" to let Axiom Lab choose.")
    return "\n".join(lines), {"workflows": rows}


def _list_playground(ctx: Context, args: dict) -> Result:
    lang = args.get("lang", "zh")
    try:
        raw = ctx.client().request("GET", "/v1/playground", params={"lang": lang})
    except VidgenError as exc:
        if exc.status not in (404, 405, 501):
            raise
        return ("This Axiom Lab server does not offer the playground catalogue yet. Use azimo_list_video_types (structured video types and their fields) "
                "and azimo_list_workflows instead."), {"available": False, "lang": lang, "items": [], "fallback": ["azimo_list_video_types", "azimo_list_workflows"]}
    raw = strip_prices(raw)
    items = playground_items(raw)
    if items is None:
        return "Axiom Lab playground catalogue:\n" + json.dumps(raw, ensure_ascii=False, indent=2), {"available": True, "lang": lang, "items": [], "catalogue": raw}
    lines = [f"Axiom Lab playground starting points ({lang}); each item's example (prompt, content_data) can be passed to azimo_create_plan:"]
    lines.extend(playground_line(item) for item in items)
    return "\n".join(lines), {"available": True, "lang": lang, "items": items}


def playground_items(raw: Any) -> Optional[list]:
    if isinstance(raw, list):
        return raw
    return next((raw[k] for k in ("data", "items", "playground", "workflows") if isinstance(raw, dict) and isinstance(raw.get(k), list)), None)


def playground_line(item: Any) -> str:
    if not isinstance(item, dict):
        return f"- {item}"
    title = item.get("name") or item.get("title") or item.get("id")
    tags = ", ".join(f"{k}={item[k]}" for k in ("video_type", "workflow", "aspect", "duration") if item.get(k))
    status = "" if item.get("available", True) else f" [not available now: {item.get('reason') or 'unavailable'}]"
    blurb = item.get("summary") or item.get("description")
    return f"- {title} (id {item.get('id')}" + (f"; {tags}" if tags else "") + ")" + status + (f": {blurb}" if blurb else "")


def _list_video_types(ctx: Context, args: dict) -> Result:
    api = ctx.client()
    types = public_video_types(api.request("GET", "/v1/video-types", params={"lang": args["lang"]}) if args.get("lang") else api.video_types())
    lines = ["Structured video types. Give azimo_create_plan a video_type and a content_data object with every field below (text values):"]
    for entry in types:
        lines.append(f"- {entry['id']} ({entry['name']}, about {entry['seconds']} s)")
        lines.extend(f"    {field['id']}: {field['name']} - {field['hint']}" for field in entry["inputs"])
    return "\n".join(lines), {"video_types": types}


def _upload_schema(allow_paths: bool) -> dict:
    inline = {
        "filename": {"type": "string", "minLength": 3, "maxLength": 200, "description": "File name with its extension, e.g. brief.pdf (no folders). Allowed: pdf, pptx, txt, md, png, jpg, jpeg, webp, mp4, mov, webm, mp3, wav, m4a."},
        "content_base64": {"type": "string", "minLength": 1, "description": "The file's bytes, base64-encoded (standard alphabet). At most 20 MB decoded."}}
    if not allow_paths:
        return _object(inline, ["filename", "content_base64"])
    return _object({"path": {"type": "string", "minLength": 1, "maxLength": 4096, "description": "Absolute or relative path of a local file to upload (at most 60 MB)."}, **inline})


def _refuse_hidden(path: Path) -> None:
    """A model chosen path must not reach dotfiles or dot-directories such as ~/.ssh: those are checked relative to home or the working directory."""
    resolved = path.resolve()
    parts = None
    for base in (Path.home(), Path.cwd()):
        try:
            parts = resolved.relative_to(base.resolve()).parts
            break
        except (ValueError, OSError, RuntimeError):
            continue
    if parts is None:
        parts = resolved.parts[-2:]
    if any(part.startswith(".") and part not in (".", "..") for part in parts):
        raise ToolError("Refusing to upload from a hidden file or folder. Copy the file somewhere visible first if you really want it uploaded.")


def _safe_filename(name: str) -> str:
    if not name or name in (".", "..") or any(c in name for c in "/\\\0") or len(name) > 200:
        raise ToolError("filename must be a plain file name without folders.")
    if not Path(name).suffix:
        raise ToolError("filename needs an extension such as .pdf or .png so Axiom Lab can tell what the file is.")
    return name


def _upload_asset(ctx: Context, args: dict) -> Result:
    api = ctx.client()
    if args.get("path") is not None:
        if not ctx.allow_paths:
            raise ToolError("Uploading by path is only available on the local server (`azimo mcp`). Send filename and content_base64 instead.")
        path = Path(args["path"]).expanduser()
        if not path.is_file():
            raise ToolError(f"No such file: {path}")
        if not os.access(path, os.R_OK):
            raise ToolError(f"The file is not readable: {path}")
        _refuse_hidden(path)
        if path.stat().st_size > MAX_PATH_UPLOAD_BYTES:
            raise ToolError("The file is larger than 60 MB, the upload limit.")
        asset = api.upload_asset(path)
    else:
        if "filename" not in args or "content_base64" not in args:
            raise ToolError("Provide " + ("`path`, or " if ctx.allow_paths else "") + "`filename` and `content_base64`.")
        filename = _safe_filename(args["filename"])
        encoded = "".join(args["content_base64"].split())
        if not encoded:
            raise ToolError("content_base64 is empty.")
        if len(encoded) * 3 // 4 > MAX_INLINE_UPLOAD_BYTES + 4:
            raise ToolError("The file is larger than 20 MB, the limit for uploads through this tool. Use the `azimo` CLI or the REST API for big files.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ToolError("content_base64 is not valid base64.") from None
        if len(data) > MAX_INLINE_UPLOAD_BYTES:
            raise ToolError("The file is larger than 20 MB, the limit for uploads through this tool. Use the `azimo` CLI or the REST API for big files.")
        asset = api.request("POST", "/v1/assets", files={"file": (filename, io.BytesIO(data))}, idempotency_key=uuid.uuid4().hex)
    asset = strip_prices(asset)
    kept = {k: asset.get(k) for k in ("id", "filename", "bytes", "sha256", "created_at") if k in asset}
    return (f"Uploaded `{kept.get('filename')}` ({kept.get('bytes')} bytes) as asset `{kept.get('id')}`. "
            "Pass this id in asset_ids of azimo_create_plan to use it as source material."), {"asset": kept}


def _list_assets(ctx: Context, args: dict) -> Result:
    page = strip_prices(ctx.client().request("GET", "/v1/assets", params={"limit": args.get("limit", 20)}))
    assets = [{k: a.get(k) for k in ("id", "filename", "bytes", "created_at")} for a in page.get("data", [])]
    lines = [f"{len(assets)} uploaded file(s)" + (" (more exist; raise limit)" if page.get("has_more") else "") + ":"]
    lines.extend(f"- `{a['id']}`  {a['filename']}  {a['bytes']} bytes  {_when(a['created_at'])}" for a in assets)
    if not assets:
        lines = ["No files uploaded in this project yet."]
    return "\n".join(lines), {"assets": assets, "has_more": bool(page.get("has_more"))}


_PLAN_SCHEMA = _object({
    "prompt": {"type": "string", "maxLength": 4000, "description": "What the film is for: the subject, who watches it and what they should do next. At least 10 characters unless video_type is set. Use only facts the user gave."},
    "workflow": {"type": "string", "enum": list(WORKFLOWS), "description": "Production workflow; omit for the default (explainer), or \"auto\" to let Axiom Lab choose. See azimo_list_workflows."},
    "video_type": {"type": "string", "enum": list(VIDEO_TYPES), "description": "Structured type (project, product, personal, corporate, name_card). Either give content_data with every field from azimo_list_video_types, or only a prompt and asset_ids; name_card (企业家名片) instead needs founder and ratio 9:16."},
    "content_data": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 2000}, "maxProperties": 12,
                     "description": "Field id -> text for the chosen video_type, e.g. {\"product_name\": \"...\"}. Only valid together with video_type."},
    "duration_max": {"type": "integer", "minimum": 5, "maximum": 180, "description": "Planned length in seconds, checked against the workflow's range "
                     "(azimo_list_workflows; 30-180 for most, profile_card 30-90). It does not cap the film: its length is confirmed after the materials "
                     "are analysed (confirm_duration)."},
    "ratio": {"type": "string", "enum": list(RATIOS), "description": "Aspect ratio: 16:9 (landscape) for every workflow but profile_card and the name_card video type, which are 9:16 (vertical)."},
    "founder": {"type": "object", "description": "name_card (企业家名片) only, and required with it: {name, company, titles: 1-3 strings, facts: 0-12 of "
                "{year?, text, source?}, photos: [{asset (an asset id, also listed in asset_ids), role: portrait|event|stage|venue|product|card|logo, "
                "caption?}] with at least one portrait, template: ink|grid|renwu, consent: true, industry?, name_pinyin?, motto?, contact?, brand_color?}. "
                "Facts, titles and a card together must cover at least two lines (a card counts as two). Length is 30-90 seconds, set from the material. "
                "The film tells these facts with weight and never adds one; use only what the user gave."},
    "asset_ids": {"type": "array", "items": {"type": "string", "pattern": "^[0-9a-f]{16}$", "description": "An asset id."}, "maxItems": 30,
                  "description": "Ids returned by azimo_upload_asset to use as source material. Every workflow but profile_card needs at least one: "
                                 "pdf, pptx, docx, png, jpg, jpeg, webp, mp4 or mov; at most 10 files, 50 MB each, 200 MB in total."},
    "music": {"type": "boolean", "description": "Background music preference. Only the profile_card studio applies it."},
    "subtitles": {"type": "boolean", "description": "Subtitle preference. Only the profile_card studio applies it; other films usually come with subtitles burned in."}})


def _create_plan(ctx: Context, args: dict) -> Result:
    api = ctx.client()
    if not args.get("video_type") and len((args.get("prompt") or "").strip()) < 10:
        raise ToolError("Provide a prompt of at least 10 characters, or a video_type with content_data.")
    if args.get("content_data") and not args.get("video_type"):
        raise ToolError("content_data only applies together with a video_type.")
    options: Dict[str, Any] = {k: args[k] for k in ("workflow", "video_type", "content_data", "duration_max", "music", "subtitles", "founder") if k in args}
    if "ratio" in args:
        options["width"], options["height"] = RATIOS[args["ratio"]]
    body = {"prompt": args.get("prompt", ""), "assets": list(args.get("asset_ids") or []), "options": options}
    plan = public_plan(api.create_plan(body, idempotency_key=uuid.uuid4().hex))
    chosen = plan["request"]["options"]
    workflow = plan["routing"]["workflow"] or chosen.get("workflow")
    ratio = _ratio_of(chosen)
    if plan["status"] == "ready":
        facts = [f"workflow {workflow}", f"planned length {chosen.get('duration_max')} s" if chosen.get("duration_max") else None, f"ratio {ratio}" if ratio else None,
                 "subtitles " + ("on" if chosen.get("subtitles") else "off"), "music " + ("on" if chosen.get("music", True) else "off"),
                 f"{len(plan['request']['assets'])} uploaded file(s)" if plan["request"]["assets"] else None]
        text = (f"Plan `{plan['id']}` is ready: " + ", ".join(f for f in facts if f) + ".\n"
                "Next: show this plan to the user and get their explicit confirmation, then call azimo_create_video with "
                f"plan_id \"{plan['id']}\". Starting production is paid.")
    else:
        lines = [f"Plan `{plan['id']}` cannot be produced yet (status: {plan['status']})."]
        if plan.get("reason"):
            lines.append(f"Reason: {plan['reason']}")
        lines.extend(f"- {r['code']}: {r['message']}" for r in plan["requirements"])
        lines.append("Adjust the inputs and call azimo_create_plan again; do not call azimo_create_video for this plan.")
        text = "\n".join(lines)
    return text, {"plan_id": plan["id"], "status": plan["status"], "workflow": workflow, "ratio": ratio, "requirements": plan["requirements"], "plan": plan}


def _create_video(ctx: Context, args: dict) -> Result:
    # One production per plan: a retried call (models retry) replays the first job instead of starting and paying for another.
    job = public_job(ctx.client().create_video(plan_id=args["plan_id"], idempotency_key="mcp-video-" + args["plan_id"]))
    if job.get("status") in ("queued", "running"):
        lead = "Production started (repeating this call returns the same video). "
    else:
        lead = "This plan already has a video. To make another film, create a new plan with azimo_create_plan. "
    return lead + _job_text(job), {"video": job}


def _get_video(ctx: Context, args: dict) -> Result:
    job = public_job(ctx.client().get_video(args["id"]))
    return _job_text(job), {"video": job, "finished": job.get("status") in TERMINAL_STATUSES}


def _wait_video(ctx: Context, args: dict) -> Result:
    api = ctx.client()
    timeout = args.get("timeout_seconds", 60)
    deadline = ctx.now() + timeout
    while True:
        job = public_job(api.get_video(args["id"]))
        if job.get("status") in TERMINAL_STATUSES:
            return _job_text(job), {"video": job, "finished": True}
        remaining = deadline - ctx.now()
        if remaining <= 0 or ctx.stop.is_set():
            return (_job_text(job).replace("Call azimo_wait_video to follow it", "It is not finished. Call azimo_wait_video again to keep waiting")), {"video": job, "finished": False}
        ctx.sleep(min(POLL_SECONDS, remaining))


def _list_videos(ctx: Context, args: dict) -> Result:
    params: Dict[str, Any] = {"limit": args.get("limit", 10)}
    if args.get("status"):
        params["status"] = args["status"]
    page = ctx.client().request("GET", "/v1/videos", params=params)
    videos = [public_job(j) for j in page.get("data", [])]
    lines = [f"{len(videos)} video(s), newest first" + (" (more exist; raise limit)" if page.get("has_more") else "") + ":"]
    lines.extend(f"- `{v['id']}`  {v['status']}  {_when(v.get('created_at'))}" for v in videos)
    if not videos:
        lines = ["No videos found."]
    return "\n".join(lines), {"videos": videos, "has_more": bool(page.get("has_more"))}


def _cancel_video(ctx: Context, args: dict) -> Result:
    outcome = strip_prices(ctx.client().cancel_video(args["id"]))
    return f"Video `{outcome.get('id')}`: {outcome.get('status')}.", {"id": outcome.get("id"), "status": outcome.get("status")}


def approve_budget(api: Any, job_id: str) -> dict:
    """Approve the spending stop of a video: check that it is waiting for exactly that, then resume it with `approve_budget`.
    The server raises the limit by what the stop needs (organization limits still apply); no amount is sent or shown. Prepaid
    (flat-priced) productions never wait for the customer: their stop is `approvable: false` and is handled by Axiom Lab."""
    job = api.get_video(job_id)
    required = (job.get("result") or {}).get("input_required") or {}
    if job.get("status") != "needs_input" or required.get("code") != "budget_approval":
        raise ToolError(f"Video `{job_id}` is not waiting for a budget approval (status: {job.get('status')}). Nothing was approved.")
    if required.get("approvable") is False:
        raise ToolError(f"Video `{job_id}` is paused for a review by the Axiom Lab team; its price does not change and there is nothing to approve.")
    return api.resume_video(job_id, approve_budget=True)


def continue_video(api: Any, job_id: str, length: str = "duration_sec", budget: str = "azimo_approve_budget") -> dict:
    """Resume a waiting video with an empty body, once its stop takes no answer: a length stop needs the confirmed length and an
    approvable spending stop its approval (an empty body would requeue it without either, so it stops again). `length` and
    `budget` name how the caller gives them."""
    job = api.get_video(job_id)
    required = (job.get("result") or {}).get("input_required") or {}
    if job.get("status") == "needs_input" and required.get("code") == "confirm_duration":
        raise ToolError(f"Video `{job_id}` waits for the film length: send {length} with the length the user confirms (suggested "
                        f"{required.get('suggested_duration_sec')} s, {required.get('min_duration_sec', 30)}-{required.get('max_duration_sec', 180)} s). "
                        "Nothing was sent.")
    if job.get("status") == "needs_input" and required.get("code") == "budget_approval" and required.get("approvable") is not False:
        raise ToolError(f"Video `{job_id}` waits for a budget approval: ask the user, then use {budget}. Nothing was sent.")
    return api.resume_video(job_id)


def _approve_budget(ctx: Context, args: dict) -> Result:
    job = public_job(approve_budget(ctx.client(), args["id"]))
    return "Budget approved; the production continues. " + _job_text(job), {"video": job}


def _resume_video(ctx: Context, args: dict) -> Result:
    if "duration_sec" in args:
        job = public_job(ctx.client().resume_video(args["id"], duration_sec=args["duration_sec"]))
        return "Confirmed the film length. " + _job_text(job), {"video": job}
    job = public_job(continue_video(ctx.client(), args["id"]))
    return "Asked the video to continue. " + _job_text(job), {"video": job}


def _get_balance(ctx: Context, args: dict) -> Result:
    wallet = ctx.client().wallet()
    currency = wallet.get("currency") or "HKD"
    balance = {"currency": currency, "balance": round((wallet.get("balance_cents") or 0) / 100, 2), "held": round((wallet.get("held_cents") or 0) / 100, 2),
               "available": round((wallet.get("available_cents") or 0) / 100, 2)}
    text = (f"Balance {_money(wallet.get('balance_cents'), currency)}; held for running productions {_money(wallet.get('held_cents'), currency)}; "
            f"Available to start new work {_money(wallet.get('available_cents'), currency)}.")
    return text, {"balance": balance}


TOOL_LIST: List[Tool] = [
    Tool("azimo_list_workflows", "List workflows",
         "List Axiom Lab's video production workflows (explainer, motion_explainer, product_pitch, profile_card, short_drama, creative_video) and whether each one is "
         "ready on this server, with supported durations and aspect ratios. Read-only.", _object({}), READ_ONLY, _list_workflows),
    Tool("azimo_list_playground", "List playground starting points",
         "List the ready-made starting points of the Axiom Lab playground (项目介绍 project intro, 人物名片 person card, 产品介绍 product film, 企业介绍 company intro, "
         "短剧广告 short-drama ad, 电商广告 e-commerce ad, and a coming-soon custom entry) with the workflow, video type, length and ratio each one uses, whether this "
         "account can start it now, and a runnable example. Use it to suggest what to make. Read-only.",
         _object({"lang": {"type": "string", "enum": ["zh", "en"], "description": "Language of the catalogue text: zh (default) or en."}}), READ_ONLY, _list_playground),
    Tool("azimo_list_video_types", "List video types",
         "List the structured video types (project, product, personal, corporate, name_card) with the exact content fields each needs. Call it before azimo_create_plan "
         "with a video_type so you can collect every required field from the user. Read-only.",
         _object({"lang": {"type": "string", "enum": ["zh", "en"], "description": "Language of the type and field names and hints: zh (default) or en."}}), READ_ONLY, _list_video_types),
    Tool("azimo_upload_asset", "Upload a file",
         "Upload a document, picture, audio or video file (pdf, pptx, docx, txt, md, png, jpg, jpeg, webp, mp4, mov, webm, mp3, wav, m4a) so a film can use it as source "
         "material; every workflow but profile_card takes only pdf, pptx, docx, png, jpg, jpeg, webp, mp4 and mov. Returns an asset id for azimo_create_plan.asset_ids. "
         "Only upload files the user asked you to use.", _upload_schema, _writes(idempotent=False), _upload_asset),
    Tool("azimo_list_assets", "List uploaded files", "List files uploaded to the current project with their asset ids. Read-only.",
         _object({"limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "How many files to return (default 20)."}}), READ_ONLY, _list_assets),
    Tool("azimo_create_plan", "Create a production plan",
         "Check a film request and freeze it as a plan: Axiom Lab validates the inputs, chooses the workflow and reports whether it can be produced. It does NOT start "
         "production. Show the returned summary to the user and get their confirmation before azimo_create_video. Give a prompt (subject, audience, desired action) "
         "or a video_type with content_data (see azimo_list_video_types), and the user's materials in asset_ids (required for every workflow but profile_card). "
         "Never invent facts the user did not provide.", _PLAN_SCHEMA, _writes(), _create_plan),
    Tool("azimo_create_video", "Start video production (paid)",
         "Start PAID video production from a ready plan. This spends the organization's balance or budget and can only be stopped by cancelling. Always show the "
         "plan to the user and get their explicit confirmation first. Calling it again with the same plan_id returns the same production instead of starting another; "
         "make a new plan for a different film. Returns the video id; then follow it with azimo_wait_video.",
         _object({"plan_id": {"type": "string", "pattern": "^[0-9a-f]{16}$", "description": "The id of a plan whose status is ready, from azimo_create_plan."}}, ["plan_id"]),
         _writes(destructive=True, idempotent=True), _create_video),
    Tool("azimo_get_video", "Get a video", "Get the current status, stage, progress and, once finished, the signed download links (film and cover; srt only where the workflow makes one) of a video. Read-only.",
         _object({"id": _VIDEO_ID}, ["id"]), READ_ONLY, _get_video),
    Tool("azimo_wait_video", "Wait for a video",
         "Wait up to timeout_seconds (maximum 110) for a video to reach a final state (complete, rejected, failed, cancelled) or to need input, returning as soon as it does. "
         "If it is still running the current status is returned with finished=false; call again to keep waiting. A film waits once in needs_input with "
         "confirm_duration after its materials are analysed: answer it with azimo_resume_video, then keep waiting. Read-only.",
         _object({"id": _VIDEO_ID, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_WAIT_SECONDS, "description": "How long to wait in seconds (1-110, default 60)."}}, ["id"]),
         READ_ONLY, _wait_video),
    Tool("azimo_list_videos", "List videos", "List recent videos in the current project, newest first, with their status. Read-only.",
         _object({"limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "How many videos to return (default 10)."},
                  "status": {"type": "string", "enum": list(JOB_STATUSES), "description": "Only videos in this status."}}), READ_ONLY, _list_videos),
    Tool("azimo_cancel_video", "Cancel a video",
         "Cancel a video that has not finished. Queued or waiting videos stop at once; a running one stops before its next paid step. Finished videos cannot be cancelled.",
         _object({"id": _VIDEO_ID}, ["id"]), _writes(destructive=True, idempotent=True), _cancel_video),
    Tool("azimo_approve_budget", "Approve extra spending",
         "Authorize extra spending for a video that stopped with an approvable budget_approval request (its status is needs_input). Only call it after the user explicitly "
         "agreed to let this production continue past its spending limit; it then resumes. Fails, changing nothing, if the video is not waiting for that approval or the "
         "stop is not approvable (a prepaid production paused for Axiom Lab's own review).",
         _object({"id": _VIDEO_ID}, ["id"]), _writes(destructive=True, idempotent=False), _approve_budget),
    Tool("azimo_resume_video", "Answer a waiting video",
         "Continue a video whose status is needs_input. For a confirm_duration stop (the materials were analysed and production waits for the film length) "
         "send duration_sec: show the user the stop's suggested_duration_sec and let them keep it or pick another length between its min_duration_sec and "
         "max_duration_sec (30-180 seconds); production then starts with that length, before its expires_at. Without duration_sec the video simply continues "
         "once the cause of the stop is fixed. For budget_approval stops use azimo_approve_budget instead. Answers in text are not accepted: for changed "
         "requirements or extra materials make a new plan.",
         _object({"id": _VIDEO_ID,
                  "duration_sec": {"type": "integer", "minimum": 30, "maximum": 180,
                                   "description": "Answer to a confirm_duration stop: the film length in seconds the user confirmed, within the stop's "
                                                  "min_duration_sec-max_duration_sec (its suggested_duration_sec unless the user chose another)."}}, ["id"]),
         _writes(), _resume_video),
    Tool("azimo_get_balance", "Get wallet balance",
         "Get the organization's wallet in Hong Kong dollars: the balance, the amount held for productions in progress and the amount available to start new ones. Read-only.", _object({}), READ_ONLY, _get_balance),
]
TOOLS: Dict[str, Tool] = {tool.name: tool for tool in TOOL_LIST}


def tool_definitions(allow_paths: bool = True) -> List[dict]:
    return [tool.definition(allow_paths) for tool in TOOL_LIST]


def _tool_result(text: str, data: Optional[dict] = None, *, error: bool = False) -> dict:
    result: Dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": error}
    if data is not None:
        result["structuredContent"] = strip_prices(data)
    return result


def call_tool(ctx: Context, name: Any, arguments: Any) -> dict:
    """Run a tool. Unknown tools are protocol errors; everything that goes wrong inside a known tool is an `isError` result."""
    tool = TOOLS.get(name) if isinstance(name, str) else None
    if tool is None:
        raise RpcError(-32602, f"Unknown tool: {name}")
    args = {} if arguments is None else arguments
    problem = validate(tool.schema(ctx.allow_paths), args)
    if problem:
        return _tool_result(f"Invalid arguments: {problem}", {"error": {"code": "invalid_arguments", "message": problem}}, error=True)
    key = getattr(ctx.api, "api_key", None)
    try:
        text, data = tool.run(ctx, args)
        return _tool_result(text, data)
    except ToolError as exc:
        return _tool_result(str(exc), exc.data or {"error": {"code": "tool_error", "message": str(exc)}}, error=True)
    except VidgenError as exc:
        return _tool_result(_redact(describe_api_error(exc), key), {"error": {"code": exc.code, "message": _redact(exc.message, key), "status": exc.status,
                                                                               "request_id": exc.request_id}}, error=True)
    except OSError as exc:  # connection refused, DNS, timeouts (requests' exceptions are OSErrors): the model can only tell the user
        detail = _redact(f"{type(exc).__name__}: {exc}", key)
        return _tool_result(f"Could not reach Axiom Lab ({getattr(ctx.api, 'base_url', 'the API')}): {detail}", {"error": {"code": "unreachable", "message": detail}}, error=True)
    except Exception as exc:  # noqa: BLE001 - a bug in a tool must not take the whole server down
        detail = _redact(f"{type(exc).__name__}: {exc}", key)
        print(f"azimo-mcp: {name} failed: {detail}", file=sys.stderr)
        return _tool_result(f"The tool failed unexpectedly ({detail}).", {"error": {"code": "internal_error", "message": detail}}, error=True)


# -- resources & prompts -----------------------------------------------------------------------------------------------------------------------
RESOURCES = [
    {"uri": "azimo://workflows", "name": "workflows", "title": "Axiom Lab workflows", "mimeType": "application/json",
     "description": "The production workflows and whether each is ready on this server."},
    {"uri": "azimo://video-types", "name": "video-types", "title": "Axiom Lab video types", "mimeType": "application/json",
     "description": "Structured video types (project, product, personal, corporate, name_card) and the content fields each needs."}]


def read_resource(ctx: Context, uri: Any) -> dict:
    if not isinstance(uri, str) or not uri:
        raise RpcError(-32602, "resources/read needs a uri")
    if uri == "azimo://workflows":
        payload = {"workflows": public_workflows(ctx.client().workflows())}
    elif uri == "azimo://video-types":
        payload = {"video_types": public_video_types(ctx.client().video_types())}
    else:
        raise RpcError(-32002, f"Resource not found: {uri}")
    return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(strip_prices(payload), ensure_ascii=False, indent=2)}]}


PROMPTS = {
    "create_product_video": ("product", "Product film", "Make a short film that sells one product to a specific buyer.", "product or brand name"),
    "create_company_intro": ("corporate", "Company introduction", "Make a company introduction film for customers or partners.", "company name"),
    "create_project_intro": ("project", "Project introduction", "Make a film that explains a project: the pain point, the solution and why it works.", "project name"),
    "create_person_card": ("personal", "Personal card", "Make a professional personal card film about one person.", "person's name"),
}


def _field_lines(ctx: Context, kind: str) -> Optional[str]:
    try:
        for entry in public_video_types(ctx.client().video_types()):
            if entry["id"] == kind and entry["inputs"]:
                return "\n".join(f"   - {f['id']}: {f['name']} - {f['hint']}" for f in entry["inputs"])
    except Exception:  # noqa: BLE001 - the prompt still works without the live field list
        return None
    return None


def get_prompt(ctx: Context, name: Any, arguments: Any) -> dict:
    if not isinstance(name, str) or name not in PROMPTS:
        raise RpcError(-32602, f"Unknown prompt: {name}")
    kind, label, description, topic_hint = PROMPTS[name]
    topic = str((arguments or {}).get("topic") or "").strip() if isinstance(arguments, dict) else ""
    fields = _field_lines(ctx, kind)
    collect = ("Collect these content fields from the user. Ask for everything that is still missing in a single message and use only facts the user gives you:\n" + fields
               if fields else f"Call azimo_list_video_types, read the inputs of type \"{kind}\" and collect every listed content field from the user in a single message; use only facts the user gives you.")
    text = "\n".join([
        f"Help the user make a {label.lower()} with Axiom Lab (video_type \"{kind}\")." + (f" The subject is: {topic}." if topic else ""),
        "Follow these steps in order:",
        "1. " + collect,
        "2. Upload the user's documents or pictures with azimo_upload_asset and keep the returned asset ids: at least one file is required (pdf, pptx, docx, "
        "png, jpg, jpeg, webp, mp4 or mov).",
        f"3. Call azimo_create_plan with video_type \"{kind}\", content_data holding those fields and asset_ids (plus duration_max if the user asked for a "
        "length; films are 16:9). Write the field values in the language the film should use (Chinese unless the user says otherwise).",
        "4. Show the user the plan summary (status, workflow, length, ratio) and ask them to confirm. Prices are agreed with Axiom Lab sales and are not shown here. If the plan "
        "is not ready, fix the missing inputs and plan again.",
        "5. Only after the user explicitly confirms, call azimo_create_video with the plan_id.",
        "6. Call azimo_wait_video repeatedly until the video finishes, then give the user the download links. When it waits with confirm_duration, show the user the "
        "suggested length and send the length they confirm with azimo_resume_video duration_sec. If it needs an approvable "
        "budget approval, ask before calling azimo_approve_budget (a stop that is not approvable is Axiom Lab's own review: just keep waiting).",
        "Reply in the user's language."])
    return {"description": description, "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}


def prompt_definitions() -> List[dict]:
    return [{"name": name, "title": label, "description": description,
             "arguments": [{"name": "topic", "description": f"The {topic_hint} the film is about (optional).", "required": False}]}
            for name, (kind, label, description, topic_hint) in PROMPTS.items()]


# -- JSON-RPC ------------------------------------------------------------------------------------------------------------------------------------
INSTRUCTIONS = (
    "Axiom Lab makes business films (project, product, company and personal introductions, ads). Workflow: azimo_list_video_types or azimo_list_playground to choose "
    "what to make; azimo_upload_asset for the user's materials (at least one file for every workflow but profile_card); azimo_create_plan to validate and freeze "
    "the request; show the plan and get the user's explicit confirmation; azimo_create_video (paid) to start; azimo_wait_video until the materials are analysed "
    "and it waits with confirm_duration; show the suggested length and send the length the user confirms with azimo_resume_video; azimo_wait_video until it "
    "finishes; give the user the download links. Never invent facts about the user's "
    "business. Prices are agreed with sales@axiomlab.online and are not shown by this server.")


def _error(message_id: Any, code: int, text: str, data: Any = None) -> dict:
    error: Dict[str, Any] = {"code": code, "message": text}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


def _params(message: dict) -> dict:
    params = message.get("params")
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise RpcError(-32602, "params must be an object")
    return params


def _initialize(ctx: Context, params: dict) -> dict:
    requested = params.get("protocolVersion")
    if not isinstance(requested, str) or not requested:
        raise RpcError(-32602, "initialize needs params.protocolVersion")
    return {"protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}, "prompts": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": "Axiom Lab", "version": __version__}, "instructions": INSTRUCTIONS}


def _dispatch(method: str, params: dict, ctx: Context) -> Any:
    if method == "initialize":
        return _initialize(ctx, params)
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tool_definitions(ctx.allow_paths)}
    if method == "tools/call":
        if params.get("arguments") is not None and not isinstance(params["arguments"], dict):
            raise RpcError(-32602, "arguments must be an object")
        return call_tool(ctx, params.get("name"), params.get("arguments"))
    if method == "resources/list":
        return {"resources": RESOURCES}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "resources/read":
        return read_resource(ctx, params.get("uri"))
    if method == "prompts/list":
        return {"prompts": prompt_definitions()}
    if method == "prompts/get":
        return get_prompt(ctx, params.get("name"), params.get("arguments"))
    raise RpcError(-32601, f"Method not found: {method}")


def handle_message(message: Any, ctx: Context) -> Optional[dict]:
    """One JSON-RPC message in, its response out; None for notifications and for responses to requests this server never sends."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(message.get("id") if isinstance(message, dict) and isinstance(message.get("id"), (str, int)) else None, -32600, "Invalid Request: expected a JSON-RPC 2.0 object")
    identifier = message.get("id")
    is_request = "id" in message
    if "method" not in message:
        if "result" in message or "error" in message:
            return None
        return _error(identifier, -32600, "Invalid Request: method is required")
    method = message["method"]
    if not isinstance(method, str) or (is_request and (isinstance(identifier, bool) or not isinstance(identifier, (str, int)))):
        return _error(None, -32600, "Invalid Request: bad method or id")
    if not is_request:
        return None  # notifications (initialized, cancelled, ...) need no reply and change nothing here
    try:
        return {"jsonrpc": "2.0", "id": identifier, "result": _dispatch(method, _params(message), ctx)}
    except RpcError as exc:
        return _error(identifier, exc.code, exc.message, exc.data)
    except ToolError as exc:  # a resource or prompt could not be built from the API
        return _error(identifier, -32603, str(exc))
    except VidgenError as exc:
        return _error(identifier, -32603, _redact(describe_api_error(exc), getattr(ctx.api, "api_key", None)))
    except Exception as exc:  # noqa: BLE001
        print(f"azimo-mcp: {method} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return _error(identifier, -32603, "Internal error")


def handle_payload(payload: Any, ctx: Context) -> Any:
    """A decoded JSON body (one message or a batch) in, the response (a dict, a list, or None when nothing is owed) out."""
    if isinstance(payload, list):
        if not payload:
            return _error(None, -32600, "Invalid Request: empty batch")
        replies = [reply for reply in (handle_message(item, ctx) for item in payload) if reply is not None]
        return replies or None
    return handle_message(payload, ctx)


SLOW_METHODS = ("tools/call", "resources/read", "prompts/get")


def is_slow(payload: Any) -> bool:
    """Whether answering may call the Axiom Lab API (and so should not block the reader that must keep answering pings)."""
    items = payload if isinstance(payload, list) else [payload]
    return any(isinstance(m, dict) and m.get("method") in SLOW_METHODS for m in items)


def serve(stdin: Any, stdout: Any, ctx: Context, *, workers: int = 4) -> int:
    """Speak MCP over newline-delimited JSON-RPC: read `stdin` until EOF, write only protocol messages to `stdout`."""
    lock = threading.Lock()

    def emit(reply: Any) -> None:
        line = json.dumps(reply, ensure_ascii=True, separators=(",", ":"))  # ASCII escapes keep every message on one physical line
        with lock:
            try:
                stdout.write(line + "\n")
                stdout.flush()
            except (BrokenPipeError, ValueError, OSError):
                ctx.stop.set()  # the client is gone

    def answer(payload: Any) -> None:
        reply = handle_payload(payload, ctx)
        if reply is not None:
            emit(reply)

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="azimo-mcp")
    try:
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                emit(_error(None, -32700, "Parse error"))
                continue
            if is_slow(payload):
                pool.submit(answer, payload)
            else:
                answer(payload)
    finally:
        ctx.stop.set()  # in-flight waits notice and return; nothing is left running after EOF
        pool.shutdown(wait=True)
    return 0


def run_stdio(settings: Any, *, transport: Any = None, stdin: Any = None, stdout: Any = None) -> int:
    """`azimo mcp`: serve on the process's stdin/stdout with the user's credentials (`settings` from vidgen_sdk.cli.resolve_settings)."""
    ctx = Context(None, allow_paths=True)
    if settings.api_key:
        ctx.api = Vidgen(settings.url, settings.api_key, settings.project, timeout=60, max_retries=2, transport=transport, sleep=ctx.stop.wait)
    protocol_out, saved = stdout or sys.stdout, sys.stdout
    reader = stdin or sys.stdin
    for stream in (reader, protocol_out):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass
    sys.stdout = sys.stderr  # a stray print() anywhere must never corrupt the protocol stream
    try:
        return serve(reader, protocol_out, ctx)
    finally:
        sys.stdout = saved


def main(argv: Optional[List[str]] = None) -> int:
    """`python -m vidgen_sdk.mcp`: same as `azimo mcp`."""
    from . import cli
    return cli.main(["mcp"] + list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
