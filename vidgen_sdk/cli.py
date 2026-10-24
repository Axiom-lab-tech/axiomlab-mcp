"""`azimo` - command line client for Axiom Lab (https://axiomlab.online): plan, start, follow and download films, and run the MCP server.

Configuration precedence: command-line flags > environment (AZIMO_API_KEY, AZIMO_URL, AZIMO_PROJECT) > the config file written by
`azimo login` (~/.config/azimo/config.json, mode 0600; XDG_CONFIG_HOME is respected and AZIMO_CONFIG overrides the path) > defaults.

Exit codes: 0 ok, 1 error, 2 usage, 3 `wait` ended without a finished film, 4 authentication (no key, or the key was rejected).
The API key is never printed. Prices are never shown: everything printed passes through `mcp.strip_prices`.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence

from . import TERMINAL_STATUSES, Vidgen, VidgenError, __version__
from . import mcp as _mcp
from .mcp import ToolError, strip_prices

DEFAULT_URL = "https://axiomlab.online"
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_NOT_COMPLETE, EXIT_AUTH = 0, 1, 2, 3, 4
_sleep = time.sleep  # tests replace this so polling loops do not really wait


class Settings(NamedTuple):
    url: str
    api_key: Optional[str]
    project: Optional[str]


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


# -- configuration ----------------------------------------------------------------------------------------------------------------------------
def config_path(environ: Optional[Dict[str, str]] = None, home: Optional[str] = None) -> str:
    env = os.environ if environ is None else environ
    if env.get("AZIMO_CONFIG"):
        return env["AZIMO_CONFIG"]
    base = env.get("XDG_CONFIG_HOME") or os.path.join(home or os.path.expanduser("~"), ".config")
    return os.path.join(base, "azimo", "config.json")


def read_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}  # a missing or damaged file behaves like no file: `azimo login` rewrites it


def write_config(path: str, data: dict) -> None:
    """Atomically replace the config file with owner-only permissions (0600); the directory is created 0700."""
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.basename(directory) == "azimo":
        os.chmod(directory, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".azimo-", dir=directory)  # created 0600
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def resolve_settings(url: Optional[str] = None, key: Optional[str] = None, project: Optional[str] = None, environ: Optional[Dict[str, str]] = None) -> Settings:
    env = os.environ if environ is None else environ
    saved = read_config(config_path(env))

    def pick(flag: Optional[str], variable: str, name: str) -> Optional[str]:
        for candidate in (flag, env.get(variable), saved.get(name)):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    return Settings((pick(url, "AZIMO_URL", "url") or DEFAULT_URL).rstrip("/"), pick(key, "AZIMO_API_KEY", "api_key"), pick(project, "AZIMO_PROJECT", "project"))


def mask_key(key: str) -> str:
    return key[:3] + "…" + key[-4:] if len(key) >= 12 else "****"


# -- output ------------------------------------------------------------------------------------------------------------------------------------
def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def render_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[str(value) if value is not None else "-" for value in row] for row in rows]
    widths = [max([_width(headers[i])] + [_width(row[i]) for row in cells]) for i in range(len(headers))]

    def line(values: Sequence[str]) -> str:
        return "  ".join(value + " " * (widths[i] - _width(value)) for i, value in enumerate(values)).rstrip()

    return "\n".join([line(list(headers))] + [line(row) for row in cells])


class Console:
    def __init__(self, out: Any, err: Any, as_json: bool):
        self.out, self.err, self.json = out, err, as_json

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def note(self, text: str) -> None:
        print(text, file=self.err)

    def emit(self, data: Any, *, compact: bool = False) -> None:
        clean = strip_prices(data)
        print(json.dumps(clean, ensure_ascii=False, separators=(",", ":")) if compact else json.dumps(clean, ensure_ascii=False, indent=2), file=self.out)


def _when(value: Any) -> str:
    return _mcp._when(value)


def _job_summary(job: dict, identifier: str) -> List[str]:
    """Human lines for a video, from the price-free public view."""
    job = _mcp.public_job(job)
    status = job.get("status", "unknown")
    head = f"Video {job.get('id', identifier)}: {status}"
    if job.get("stage") and job["stage"] != status:
        head += f" ({job['stage']})"
    if job.get("progress") is not None and status in ("queued", "running"):
        head += f" {job['progress']}%"
    lines = [head]
    result = job.get("result") or {}
    if job.get("error"):
        lines.append(f"  error: {job['error']}")
    if result.get("duration"):
        lines.append(f"  length: {float(result['duration']):.1f} s")
    for name, url in (result.get("files") or {}).items():
        lines.append(f"  {name}: {url}")
    waiting = result.get("input_required")
    if waiting:
        lines.append(f"  waiting for input ({waiting['code']}): {waiting['message']}")
        if waiting.get("approvable") is not False:  # a non-approvable stop is Axiom Lab's own review: nothing to answer
            answer = {"budget_approval": "--approve-budget", "confirm_duration": "--duration <seconds>"}.get(waiting["code"], "")
            lines.append(f"  answer with: azimo resume {identifier} {answer}".rstrip())
    elif status in ("queued", "running"):
        lines.append(f"  follow it with: azimo wait {identifier}")
    elif status == "complete":
        lines.append(f"  save the film with: azimo download {identifier}")
    return lines


def _plan_summary(plan: dict) -> List[str]:
    view = _mcp.public_plan(plan)
    options = view["request"]["options"]
    if view["status"] == "ready":
        ratio = _mcp._ratio_of(options)
        rows = [("workflow", view["routing"]["workflow"] or options.get("workflow")),
                ("length", f"planned {options['duration_max']} s" if options.get("duration_max") else None),
                ("ratio", ratio), ("subtitles", "on" if options.get("subtitles") else "off"), ("music", "on" if options.get("music", True) else "off"),
                ("video type", options.get("video_type")), ("files", len(view["request"]["assets"]) or None)]
        return [f"Plan {view['id']} is ready"] + [f"  {name:<10} {value}" for name, value in rows if value is not None]
    lines = [f"Plan {view['id']} cannot be produced yet ({view['status']})"]
    if view.get("reason"):
        lines.append(f"  reason: {view['reason']}")
    lines.extend(f"  {item['code']}: {item['message']}" for item in view["requirements"])
    return lines


# -- session ----------------------------------------------------------------------------------------------------------------------------------
class Session:
    def __init__(self, args: argparse.Namespace, console: Console, transport: Any):
        self.args, self.console, self.transport = args, console, transport
        self.settings = resolve_settings(getattr(args, "url", None), getattr(args, "key", None), getattr(args, "project", None))
        self._api: Optional[Vidgen] = None

    @property
    def api(self) -> Vidgen:
        if self._api is None:
            if not self.settings.api_key:
                raise CliError("Not logged in: run `azimo login`, or set AZIMO_API_KEY.", EXIT_AUTH)
            self._api = Vidgen(self.settings.url, self.settings.api_key, self.settings.project, transport=self.transport, sleep=lambda s: _sleep(s))
        return self._api


def _is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _read_key() -> str:
    """The key typed at a terminal prompt (hidden), or one line of piped input (`echo $KEY | azimo login`)."""
    if _is_tty():
        return getpass.getpass("Axiom Lab API key (input hidden): ")
    return sys.stdin.readline()


def _new_key() -> str:
    return uuid.uuid4().hex


# -- commands ----------------------------------------------------------------------------------------------------------------------------------
def cmd_login(s: Session) -> int:
    args, environ = s.args, os.environ
    path = config_path(environ)
    saved = read_config(path)
    url = (getattr(args, "url", None) or environ.get("AZIMO_URL") or saved.get("url") or DEFAULT_URL).rstrip("/")
    project = getattr(args, "project", None) or (saved.get("project") if saved.get("url", DEFAULT_URL).rstrip("/") == url else None)
    key = (getattr(args, "key", None) or _read_key()).strip()
    if not key:
        raise CliError("No API key given.", EXIT_USAGE)
    client = Vidgen(url, key, project, transport=s.transport, sleep=lambda seconds: _sleep(seconds))
    try:
        me = client.me()
    except VidgenError as exc:
        if exc.status == 401:
            raise CliError(f"The API key was rejected by {url}. Nothing was saved.", EXIT_AUTH)
        raise
    try:
        write_config(path, {"url": url, "api_key": key, **({"project": project} if project else {})})
    except OSError as exc:
        raise CliError(f"The key is valid but could not be saved to {path}: {exc}")
    s.console.say(f"Logged in to {url} as {me.get('role')} with key {mask_key(key)} (project {me.get('project_id')}).")
    s.console.say(f"Saved to {path} (owner-only).")
    return EXIT_OK


def cmd_logout(s: Session) -> int:
    path = config_path(os.environ)
    saved = read_config(path)
    if "api_key" in saved:
        saved.pop("api_key")
        if saved:
            write_config(path, saved)
        else:
            try:
                os.unlink(path)
            except OSError:
                pass
        s.console.say("Logged out: the stored API key was removed.")
    else:
        s.console.say("No stored API key.")
    if os.environ.get("AZIMO_API_KEY"):
        s.console.note("AZIMO_API_KEY is still set in your environment and will keep being used.")
    return EXIT_OK


def cmd_whoami(s: Session) -> int:
    me = s.api.me()
    if s.console.json:
        s.console.emit({**me, "url": s.settings.url})
        return EXIT_OK
    permissions = ", ".join(name for name, allowed in (me.get("permissions") or {}).items() if allowed) or "read"
    for label, value in (("URL", s.settings.url), ("Key", mask_key(s.settings.api_key or "")), ("Role", me.get("role")), ("Organization", me.get("organization_id")),
                         ("Project", me.get("project_id")), ("Billing", me.get("billing_mode")), ("Can", permissions)):
        s.console.say(f"{label + ':':<14}{value}")
    return EXIT_OK


def cmd_workflows(s: Session) -> int:
    rows = _mcp.public_workflows(s.api.workflows())
    if s.console.json:
        s.console.emit({"workflows": {row["name"]: {k: v for k, v in row.items() if k != "name"} for row in rows}})
        return EXIT_OK
    table = [(row["name"], "yes" if row["configured"] else "no", "-".join(str(n) for n in row.get("duration_seconds", [])) or "-",
              " ".join(row.get("aspect_ratios", [])) or "-", row["description"]) for row in rows]
    s.console.say(render_table(("WORKFLOW", "CONFIGURED", "SECONDS", "RATIOS", "WHAT IT MAKES"), table))
    return EXIT_OK


def _print_types(s: Session, raw: Any) -> None:
    types = _mcp.public_video_types(raw)
    s.console.say(render_table(("TYPE", "NAME", "SECONDS", "FIELDS (--field id=value)"),
                               [(t["id"], t["name"], t["seconds"], ", ".join(i["id"] for i in t["inputs"])) for t in types]))


def cmd_types(s: Session) -> int:
    lang = getattr(s.args, "lang", None)
    raw = s.api.request("GET", "/v1/video-types", params={"lang": lang} if lang else None)
    if s.console.json:
        s.console.emit(raw)
    else:
        _print_types(s, raw)
    return EXIT_OK


def cmd_playground(s: Session) -> int:
    lang = getattr(s.args, "lang", None) or "zh"
    try:
        raw = s.api.request("GET", "/v1/playground", params={"lang": lang})
    except VidgenError as exc:
        if exc.status not in (404, 405, 501):
            raise
        s.console.note("This server does not offer the playground catalogue yet (GET /v1/playground is not available). "
                       "Showing the video types instead; see also `azimo types` and `azimo workflows`.")
        return cmd_types(s)
    if s.console.json:
        s.console.emit(raw)
        return EXIT_OK
    raw = strip_prices(raw)
    items = _mcp.playground_items(raw)
    if items is None:
        s.console.say(json.dumps(raw, ensure_ascii=False, indent=2))
        return EXIT_OK
    rows = []
    for item in (i for i in items if isinstance(i, dict)):
        available = "yes" if item.get("available", True) else "no (" + str(item.get("reason") or "unavailable") + ")"
        rows.append((item.get("id"), item.get("name") or item.get("title"), item.get("video_type"), item.get("workflow"), item.get("aspect"),
                     item.get("duration"), available, item.get("summary") or item.get("description")))
    s.console.say(render_table(("ID", "NAME", "TYPE", "WORKFLOW", "RATIO", "SECONDS", "AVAILABLE", "SUMMARY"), rows))
    s.console.say("Try one: azimo plan --type TYPE --field ... or azimo plan --prompt \"...\" (`azimo playground --json` includes runnable examples).")
    return EXIT_OK


def cmd_assets(s: Session) -> int:
    if s.args.assets_command == "list":
        page = s.api.request("GET", "/v1/assets", params={"limit": s.args.limit})
        if s.console.json:
            s.console.emit(page)
        else:
            rows = [(a["id"], a["filename"], a["bytes"], _when(a.get("created_at"))) for a in page.get("data", [])]
            s.console.say(render_table(("ID", "FILE", "BYTES", "UPLOADED"), rows) if rows else "No files uploaded in this project yet.")
        return EXIT_OK
    failed = False
    for name in s.args.files:
        path = Path(name).expanduser()
        if not path.is_file() or not os.access(path, os.R_OK):
            s.console.note(f"Cannot upload {name}: no such readable file.")
            failed = True
            continue
        asset = s.api.upload_asset(path)
        if s.console.json:
            s.console.emit(asset, compact=True)
        else:
            s.console.say(f"{asset['id']}  {asset['filename']}  {asset['bytes']} bytes")
    return EXIT_ERROR if failed else EXIT_OK


def plan_body(args: argparse.Namespace) -> dict:
    prompt = args.prompt
    if getattr(args, "prompt_file", None):
        try:
            prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise CliError(f"Cannot read --prompt-file: {exc}", EXIT_USAGE)
    prompt = (prompt or "").strip()
    if not args.type and len(prompt) < 10:
        raise CliError("Give --prompt or --prompt-file (at least 10 characters), or a --type with --field values. See `azimo types`.", EXIT_USAGE)
    if args.field and not args.type:
        raise CliError("--field needs --type (see `azimo types` for the fields of each type).", EXIT_USAGE)
    options: Dict[str, Any] = {}
    if args.workflow:
        options["workflow"] = args.workflow
    if args.type:
        options["video_type"] = args.type
    if args.field:
        options["content_data"] = dict(args.field)
    if args.duration:
        options["duration_max"] = args.duration
    if args.ratio:
        options["width"], options["height"] = _mcp.RATIOS[args.ratio]
    if args.music is not None:
        options["music"] = args.music
    if args.subtitles is not None:
        options["subtitles"] = args.subtitles
    return {"prompt": prompt, "assets": list(args.asset or []), "options": options}


def _make_plan(s: Session) -> dict:
    return s.api.create_plan(plan_body(s.args), idempotency_key=_new_key())


def cmd_plan(s: Session) -> int:
    plan = _make_plan(s)
    ready = (plan.get("data") or {}).get("status") == "ready"
    if s.console.json:
        s.console.emit(plan)
    else:
        for line in _plan_summary(plan):
            s.console.say(line)
        if ready:
            s.console.say(f"Start it (paid) with: azimo create --plan {plan['id']}")
    return EXIT_OK if ready else EXIT_ERROR


def plan_video_key(plan_id: str) -> str:
    """A plan starts at most one production: the key is derived from the plan, so re-running a command never charges twice."""
    return "cli-video-" + plan_id


def cmd_create(s: Session) -> int:
    job = s.api.create_video(plan_id=s.args.plan, idempotency_key=plan_video_key(s.args.plan))
    if s.console.json:
        s.console.emit(job)
    else:
        s.console.say("Production started.")
        for line in _job_summary(job, job["id"]):
            s.console.say(line)
    return EXIT_OK


def cmd_run(s: Session) -> int:
    plan = _make_plan(s)
    ready = (plan.get("data") or {}).get("status") == "ready"
    if not s.console.json:
        for line in _plan_summary(plan):
            s.console.say(line)
    if not ready:
        if s.console.json:
            s.console.emit({"plan": plan})
        return EXIT_ERROR
    if not s.args.yes and _is_tty():
        answer = input("Start production now? This is paid. [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            s.console.note(f"Nothing was started. The plan stays valid: azimo create --plan {plan['id']}")
            return EXIT_ERROR
    job = s.api.create_video(plan_id=plan["id"], idempotency_key=plan_video_key(plan["id"]))
    if s.console.json:
        s.console.emit({"plan": plan, "video": job})
    else:
        s.console.say("Production started.")
        for line in _job_summary(job, job["id"]):
            s.console.say(line)
    return EXIT_OK


def cmd_status(s: Session) -> int:
    job = s.api.get_video(s.args.id)
    if s.console.json:
        s.console.emit(job)
    else:
        for line in _job_summary(job, s.args.id):
            s.console.say(line)
    return EXIT_OK


def cmd_wait(s: Session) -> int:
    identifier, deadline, last = s.args.id, time.monotonic() + s.args.timeout, None
    while True:
        job = s.api.get_video(identifier)
        state = (job.get("status"), job.get("stage"), job.get("progress"))
        if state != last and not s.console.json and job.get("status") not in TERMINAL_STATUSES:
            s.console.note(f"{job.get('status')}" + (f" - {job['stage']}" if job.get("stage") else "") + (f" {job['progress']}%" if job.get("progress") is not None else ""))
        last = state
        if job.get("status") in TERMINAL_STATUSES:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            s.console.note(f"Still {job.get('status')} after {s.args.timeout:g} s; keep waiting with: azimo wait {identifier}")
            if s.console.json:
                s.console.emit(job)
            return EXIT_NOT_COMPLETE
        _sleep(min(s.args.interval, remaining))
    if s.console.json:
        s.console.emit(job)
    else:
        for line in _job_summary(job, identifier):
            s.console.say(line)
    return EXIT_OK if job.get("status") == "complete" else EXIT_NOT_COMPLETE


def cmd_list(s: Session) -> int:
    params: Dict[str, Any] = {"limit": s.args.limit}
    if getattr(s.args, "status", None):
        params["status"] = s.args.status
    page = s.api.request("GET", "/v1/videos", params=params)
    if s.console.json:
        s.console.emit(page)
        return EXIT_OK
    rows = [(v["id"], v.get("status"), v.get("stage"), None if v.get("progress") is None else f"{v['progress']}%", _when(v.get("created_at")),
             v.get("requested_workflow")) for v in page.get("data", [])]
    s.console.say(render_table(("ID", "STATUS", "STAGE", "DONE", "CREATED", "WORKFLOW"), rows) if rows else "No videos yet. Start with `azimo plan`.")
    return EXIT_OK


FILE_SUFFIX = {"film": ".mp4", "srt": ".srt", "cover": "-cover.jpg"}


def cmd_download(s: Session) -> int:
    job = s.api.get_video(s.args.id)
    if job.get("status") != "complete":
        raise CliError(f"Video {s.args.id} is not complete (status: {job.get('status')}); wait for it with `azimo wait {s.args.id}`.")
    files = (job.get("result") or {}).get("files") or {}
    names = list(FILE_SUFFIX) if s.args.name == "all" else [s.args.name]
    directory = Path(s.args.dir).expanduser()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CliError(f"Cannot create {directory}: {exc}")
    failed, saved = False, []
    for name in names:
        url = files.get(name)
        if not url:
            if s.args.name != "all":
                s.console.note(f"This video has no {name} file.")
                failed = True
            continue
        if url.startswith("/"):
            url = s.api.base_url + url
        data = s.api.download_file(url)
        target = directory / (s.args.id + FILE_SUFFIX[name])
        try:
            target.write_bytes(data)
        except OSError as exc:
            raise CliError(f"Cannot write {target}: {exc}")
        saved.append({"name": name, "path": str(target), "bytes": len(data)})
        if not s.console.json:
            s.console.say(f"{target}  ({len(data)} bytes)")
    if s.console.json:
        s.console.emit({"files": saved})
    return EXIT_ERROR if failed or not saved else EXIT_OK


def cmd_cancel(s: Session) -> int:
    outcome = s.api.cancel_video(s.args.id)
    if s.console.json:
        s.console.emit(outcome)
    else:
        s.console.say(f"Video {outcome.get('id', s.args.id)}: {outcome.get('status')}")
    return EXIT_OK


def cmd_resume(s: Session) -> int:
    try:
        if s.args.approve_budget:
            job = _mcp.approve_budget(s.api, s.args.id)
        elif s.args.duration:
            job = s.api.confirm_duration(s.args.id, s.args.duration)
        else:
            job = _mcp.continue_video(s.api, s.args.id, length="--duration SECONDS", budget="--approve-budget")
    except ToolError as exc:
        raise CliError(str(exc))
    if s.console.json:
        s.console.emit(job)
    else:
        for line in _job_summary(job, s.args.id):
            s.console.say(line)
    return EXIT_OK


def cmd_balance(s: Session) -> int:
    wallet = s.api.wallet()
    if s.console.json:
        s.console.emit(wallet)
        return EXIT_OK
    currency = wallet.get("currency") or "HKD"
    for label, key in (("Balance", "balance_cents"), ("Held", "held_cents"), ("Available", "available_cents")):
        s.console.say(f"{label + ':':<11}{_mcp._money(wallet.get(key), currency)}")
    return EXIT_OK


def cmd_mcp(s: Session) -> int:
    return _mcp.run_stdio(s.settings, transport=s.transport, stdout=s.console.out)


def cmd_version(s: Session) -> int:
    s.console.say(f"azimo {__version__}")
    return EXIT_OK


# -- argument parsing --------------------------------------------------------------------------------------------------------------------------
def _bounded_int(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number")
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value
    return parse


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number")
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def _field(text: str) -> tuple:
    key, separator, value = text.partition("=")
    if not separator or not key.strip():
        raise argparse.ArgumentTypeError("expected key=value")
    return key.strip(), value


def _plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompt", help="what the film is for: subject, audience, desired action")
    parser.add_argument("--prompt-file", help="read the prompt from this UTF-8 file")
    parser.add_argument("--workflow", choices=_mcp.WORKFLOWS, help="default explainer; auto lets Axiom Lab choose (see `azimo workflows`)")
    parser.add_argument("--type", choices=_mcp.VIDEO_TYPES, help="structured video type (see `azimo types`); needs --field values")
    parser.add_argument("--duration", type=_bounded_int(5, 180), help="planned length in seconds, checked against the workflow's range "
                        "(`azimo workflows`; 30-180 for most, profile_card 30-90); not a cap: the film length is confirmed later with "
                        "`azimo resume --duration`")
    parser.add_argument("--ratio", choices=list(_mcp.RATIOS), help="aspect ratio (16:9; profile_card 9:16)")
    parser.add_argument("--asset", action="append", metavar="ID", help="uploaded file id to use as material (repeatable; see `azimo assets upload`)")
    parser.add_argument("--music", action=argparse.BooleanOptionalAction, default=None, help="background music preference (applied by profile_card only)")
    parser.add_argument("--subtitles", action=argparse.BooleanOptionalAction, default=None, help="subtitle preference (applied by profile_card only)")
    parser.add_argument("--field", action="append", type=_field, metavar="KEY=VALUE", help="content field of the --type (repeatable)")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print machine-readable JSON")
    common.add_argument("--url", default=argparse.SUPPRESS, help=f"Axiom Lab URL (env AZIMO_URL, default {DEFAULT_URL})")
    common.add_argument("--key", "--api-key", dest="key", default=argparse.SUPPRESS, metavar="KEY",
                        help="API key; prefer AZIMO_API_KEY or `azimo login` (a flag is visible in process listings)")
    common.add_argument("--project", default=argparse.SUPPRESS, help="project id (env AZIMO_PROJECT)")
    parser = argparse.ArgumentParser(prog="azimo", description="Command line client for Axiom Lab: plan, start and download films; run the MCP server.",
                     epilog="Exit codes: 0 ok, 1 error, 2 usage, 3 wait ended without a finished film, 4 authentication.", parents=[common])
    parser.add_argument("--version", action="version", version=f"azimo {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, help_text: str, handler: Callable[[Session], int]) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text, description=help_text, parents=[common])
        command.set_defaults(handler=handler)
        return command

    add("login", "save an API key (verified first) to the config file", cmd_login)
    add("logout", "remove the stored API key", cmd_logout)
    add("whoami", "show who the key belongs to", cmd_whoami)
    add("workflows", "list production workflows and whether they are configured", cmd_workflows)
    playground = add("playground", "list the ready-made starting points", cmd_playground)
    playground.add_argument("--lang", choices=("zh", "en"), default=argparse.SUPPRESS)
    types = add("types", "list structured video types and their fields", cmd_types)
    types.add_argument("--lang", choices=("zh", "en"), default=argparse.SUPPRESS, help="language of names and hints (default: the server's, zh)")
    assets = add("assets", "upload and list source files", cmd_assets)
    assets_sub = assets.add_subparsers(dest="assets_command", metavar="ACTION", required=True)
    upload = assets_sub.add_parser("upload", help="upload files", parents=[common])
    upload.add_argument("files", nargs="+", metavar="FILE")
    listing = assets_sub.add_parser("list", help="list uploaded files", parents=[common])
    listing.add_argument("--limit", type=_bounded_int(1, 100), default=20)
    _plan_arguments(add("plan", "check a request and freeze it as a plan (does not start production)", cmd_plan))
    create = add("create", "start paid production from a ready plan", cmd_create)
    create.add_argument("--plan", required=True, metavar="PLAN_ID")
    run = add("run", "plan, show the plan, then start production", cmd_run)
    _plan_arguments(run)
    run.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation on a terminal")
    add("status", "show one video", cmd_status).add_argument("id")
    wait = add("wait", "wait for a video (exit 3 unless it completes)", cmd_wait)
    wait.add_argument("id")
    wait.add_argument("--timeout", type=_positive_float, default=7200.0, help="seconds to wait (default 7200)")
    wait.add_argument("--interval", type=_positive_float, default=10.0, help="seconds between checks (default 10)")
    lst = add("list", "list recent videos", cmd_list)
    lst.add_argument("--limit", type=_bounded_int(1, 200), default=20)
    lst.add_argument("--status", choices=_mcp.JOB_STATUSES)
    download = add("download", "download the film, subtitles or cover of a finished video", cmd_download)
    download.add_argument("id")
    download.add_argument("--dir", default=".", help="target directory (default .)")
    download.add_argument("--name", choices=("film", "srt", "cover", "all"), default="film",
                          help="film (default), srt (only if the finished video lists one; current workflows deliver the film and cover), cover, "
                               "or all the video has")
    add("cancel", "cancel a video that has not finished", cmd_cancel).add_argument("id")
    resume = add("resume", "answer a video that needs input", cmd_resume)
    resume.add_argument("id")
    answer = resume.add_mutually_exclusive_group()
    answer.add_argument("--approve-budget", action="store_true", help="approve the spending stop (authorizes extra spending)")
    answer.add_argument("--duration", type=_bounded_int(30, 180), metavar="SECONDS",
                        help="confirm the film length (30-180 s) of a video waiting with confirm_duration")
    add("balance", "show the wallet balance", cmd_balance)
    add("mcp", "run the MCP server on stdin/stdout (for AI assistants)", cmd_mcp)
    add("version", "print the version", cmd_version)
    return parser


def main(argv: Optional[Sequence[str]] = None, *, transport: Any = None, stdout: Any = None, stderr: Any = None) -> int:
    out, err = stdout or sys.stdout, stderr or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (EXIT_OK if exc.code is None else EXIT_USAGE)
    if not getattr(args, "command", None):
        parser.print_help(err)
        return EXIT_USAGE
    console = Console(out, err, bool(getattr(args, "json", False)))
    session = Session(args, console, transport)
    key = session.settings.api_key
    try:
        return args.handler(session)
    except CliError as exc:
        console.note(f"Error: {exc}")
        return exc.code
    except VidgenError as exc:
        if exc.status == 401:
            console.note("Error: the API key was rejected (unauthenticated). Run `azimo login` with a valid key.")
            return EXIT_AUTH
        message = f"Error: {exc.code}: {exc.message}"
        if exc.request_id:
            message += f" (request {exc.request_id})"
        if exc.status == 429 and exc.retry_after:
            message += f". Retry in {int(exc.retry_after)} s."
        console.note(_mcp._redact(message, key))
        return EXIT_ERROR
    except KeyboardInterrupt:
        console.note("Interrupted.")
        return EXIT_ERROR
    except OSError as exc:  # requests' connection and timeout errors
        console.note(_mcp._redact(f"Error: could not reach {session.settings.url}: {type(exc).__name__}: {exc}", key))
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
