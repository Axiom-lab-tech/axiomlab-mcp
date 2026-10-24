"""Official Python client for the Axiom Lab (vidgen) API. Depends only on `requests`.

Errors: every non-2xx raises VidgenError with the machine-readable `code` when the server sends one
(`{"error": {"code", "message", "details", "request_id"}}`) and a best-effort mapping of the legacy `{"detail": …}` shape.
Retries: GET/PUT/DELETE and POSTs that carry an Idempotency-Key are retried on 429/502/503/504 and transport errors with
exponential backoff and jitter, honouring Retry-After. Other POSTs are never resent automatically.
"""
from __future__ import annotations

import random
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

__version__ = "0.6.0"
__all__ = ["Vidgen", "VidgenError", "TERMINAL_STATUSES"]

TERMINAL_STATUSES = ("complete", "rejected", "failed", "cancelled", "needs_input")
RETRY_STATUSES = (429, 500, 502, 503, 504)


class VidgenError(Exception):
    """A non-2xx API response."""

    def __init__(self, status: int, body: Any, headers: dict | None = None):
        self.status, self.body, self.headers = status, body, dict(headers or {})
        self.code, self.message, self.details, self.request_id = _parse_error(status, body)
        super().__init__(f"vidgen HTTP {status} {self.code}: {self.message}")

    @property
    def retry_after(self) -> float | None:
        value = self.headers.get("Retry-After") or self.headers.get("retry-after")
        try:
            return float(value) if value is not None else None
        except ValueError:
            return None


def _contact_sales() -> VidgenError:
    return VidgenError(403, {"error": {"code": "contact_sales", "message": "Pricing is agreed per customer through sales; contact sales@axiomlab.online.",
                                       "details": None, "request_id": None}})


def _parse_error(status: int, body: Any) -> tuple[str, str, Any, str | None]:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):  # unified schema
            return str(err.get("code") or f"http_{status}"), str(err.get("message") or ""), err.get("details"), err.get("request_id")
        detail = body.get("detail", err)
        if isinstance(detail, dict) and detail.get("requirements"):
            first = detail["requirements"][0]
            return str(first.get("code") or f"http_{status}"), str(first.get("message") or ""), detail["requirements"], None
        if isinstance(detail, list):  # pydantic validation
            return "validation_error", "; ".join(str(d.get("msg", d)) for d in detail if isinstance(d, dict)) or "invalid request", detail, None
        if isinstance(detail, str):
            return _legacy_code(status, detail), detail, None, None
        if isinstance(detail, dict):
            return str(detail.get("code") or f"http_{status}"), str(detail.get("message") or detail), detail, None
    return f"http_{status}", str(body)[:300] if body is not None else "", None, None


def _legacy_code(status: int, detail: str) -> str:
    text = detail.lower()
    if status == 401:
        return "unauthenticated"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 409:
        return "idempotency_conflict" if "idempotency" in text else "conflict"
    if status == 402:
        return "insufficient_balance" if "balance" in text else "budget_exceeded"
    if status == 413:
        return "storage_quota_exceeded" if "quota" in text else "payload_too_large"
    if status == 415:
        return "unsupported_media_type"
    if status == 422:
        return "validation_error"
    if status == 429:
        return "rate_limited"
    if status == 503:
        return "backend_unconfigured"
    return f"http_{status}"


class Vidgen:
    """Synchronous client. `transport` is any object with requests' `.request(method, url, **kw)` signature (tests inject one)."""

    def __init__(self, base_url: str, api_key: str, project_id: str | None = None, *, timeout: float = 90, max_retries: int = 3,
                 transport=None, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.api_key, self.project_id, self.timeout, self.max_retries = api_key, project_id, timeout, max_retries
        if transport is None:
            import requests
            transport = requests.Session()
        self.transport, self._sleep = transport, sleep

    # -- plumbing ---------------------------------------------------------------------------------------------------
    def request(self, method: str, path: str, *, idempotency_key: str | None = None, params: dict | None = None, json: Any = None,
                files: Any = None, data: Any = None, raw: bool = False) -> Any:
        headers = {"Authorization": "Bearer " + self.api_key, "User-Agent": f"vidgen-sdk-python/{__version__}"}
        if self.project_id:
            headers["X-Project-ID"] = self.project_id
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        retryable = method.upper() in ("GET", "PUT", "DELETE", "HEAD") or bool(idempotency_key)
        positions = []
        for item in (files or {}).values():
            stream = item[1] if isinstance(item, tuple) else item
            if hasattr(stream, "tell") and hasattr(stream, "seek"):
                positions.append((stream, stream.tell()))
        attempt = 0
        while True:
            for stream, position in positions:
                stream.seek(position)
            try:
                response = self.transport.request(method, self.base_url + path, headers=headers, params=params, json=json, files=files,
                                                  data=data, timeout=self.timeout)
            except Exception:  # noqa: BLE001 — transport errors (timeouts, resets) are retried when safe
                if not retryable or attempt >= self.max_retries:
                    raise
                self._sleep(self._backoff(attempt, None))
                attempt += 1
                continue
            if 200 <= response.status_code < 300:
                return response.content if raw else (response.json() if response.content else None)
            try:
                body = response.json()
            except ValueError:
                body = (response.content or b"")[:300].decode("utf-8", "replace")
            error = VidgenError(response.status_code, body, response.headers)
            if retryable and response.status_code in RETRY_STATUSES and attempt < self.max_retries:
                self._sleep(self._backoff(attempt, error.retry_after))
                attempt += 1
                continue
            raise error

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, 60.0)
        return min(30.0, (2 ** attempt) * 0.5 + random.uniform(0, 0.25))

    def _paged(self, path: str, params: dict | None = None) -> Iterator[dict]:
        """Follow `next_cursor` (events/audit/usage-style cursors) or stop after one page for limit-only lists."""
        params = dict(params or {})
        while True:
            page = self.request("GET", path, params=params)
            for item in page.get("data", []):
                yield item
            cursor = page.get("next_cursor")
            if page.get("has_more") is False or not cursor or not page.get("data") or cursor == params.get("after"):
                return
            params["after"] = cursor

    # -- identity & organization ------------------------------------------------------------------------------------
    def me(self) -> dict:
        return self.request("GET", "/v1/me")

    def list_projects(self) -> list[dict]:
        return list(self._paged("/v1/projects"))

    def create_project(self, name: str, *, idempotency_key: str | None = None) -> dict:
        return self.request("POST", "/v1/projects", json={"name": name}, idempotency_key=idempotency_key or uuid.uuid4().hex)

    def delete_project(self, project_id: str) -> dict:
        return self.request("DELETE", f"/v1/projects/{project_id}")

    def list_api_keys(self) -> list[dict]:
        return list(self._paged("/v1/api-keys"))

    def create_api_key(self, name: str, role: str = "editor", project_id: str | None = None, *, monthly_budget_usd: float | None = None,
                       rate_per_hour: int | None = None, idempotency_key: str | None = None) -> dict:
        """A key follows its organization's limits unless `monthly_budget_usd` / `rate_per_hour` set a tighter cap for this key."""
        body = {"name": name, "role": role, "project_id": project_id, "monthly_budget_usd": monthly_budget_usd, "rate_per_hour": rate_per_hour}
        return self.request("POST", "/v1/api-keys", json={k: v for k, v in body.items() if v is not None or k == "project_id"}, idempotency_key=idempotency_key or uuid.uuid4().hex)

    def rotate_api_key(self, key_id: str, grace_seconds: int = 0, *, idempotency_key: str | None = None) -> dict:
        return self.request("POST", f"/v1/api-keys/{key_id}/rotate", params={"grace_seconds": grace_seconds}, idempotency_key=idempotency_key or uuid.uuid4().hex)

    def revoke_api_key(self, key_id: str) -> dict:
        return self.request("DELETE", f"/v1/api-keys/{key_id}")

    def audit(self, after: int = 0, limit: int = 50) -> Iterator[dict]:
        return self._paged("/v1/audit", {"after": after, "limit": limit})

    # -- capabilities, usage, wallet -----------------------------------------------------------------------------------
    def workflows(self) -> dict:
        return self.request("GET", "/v1/workflows")

    def video_types(self) -> list[dict]:
        return self.request("GET", "/v1/video-types")["data"]

    def quote(self, options: dict | None = None) -> dict:
        """Removed: prices are agreed per customer through sales and the API never quotes them. Always raises the `contact_sales`
        error (HTTP 403) the server answers `POST /v1/quote` with."""
        raise _contact_sales()

    def route(self, request: dict, *, idempotency_key: str | None = None) -> dict:
        return self.request("POST", "/v1/route", json=request, idempotency_key=idempotency_key or uuid.uuid4().hex)

    def prices(self) -> dict:
        """Removed: there is no published price list. Always raises the `contact_sales` error (HTTP 403)."""
        raise _contact_sales()

    def usage(self, **params) -> dict:
        """Jobs and the amount billed (`billed_cents` in `currency`, HKD), never provider cost. params: from_ (unix s), to, group_by in
        none|day|key|workflow|job, after, limit."""
        if "from_" in params:
            params["from"] = params.pop("from_")
        return self.request("GET", "/v1/usage", params=params or None)

    def wallet(self) -> dict:
        return self.request("GET", "/v1/wallet")

    def wallet_entries(self, after: str | None = None, limit: int = 50) -> Iterator[dict]:
        return self._paged("/v1/wallet/entries", {"after": after, "limit": limit})

    # -- assets --------------------------------------------------------------------------------------------------------
    def upload_asset(self, path, *, idempotency_key: str | None = None) -> dict:
        path = Path(path)
        with path.open("rb") as file:
            return self.request("POST", "/v1/assets", files={"file": (path.name, file)}, idempotency_key=idempotency_key or uuid.uuid4().hex)

    def list_assets(self, limit: int = 50) -> list[dict]:
        return list(self._paged("/v1/assets", {"limit": limit}))

    def get_asset(self, asset_id: str) -> dict:
        return self.request("GET", f"/v1/assets/{asset_id}")

    def download_asset(self, asset_id: str) -> bytes:
        return self.request("GET", f"/v1/assets/{asset_id}/file", raw=True)

    def delete_asset(self, asset_id: str) -> dict:
        return self.request("DELETE", f"/v1/assets/{asset_id}")

    # -- brands, templates -----------------------------------------------------------------------------------------------
    def create_brand(self, brand: dict, *, idempotency_key: str | None = None) -> dict:
        return self.request("POST", "/v1/brands", json=brand, idempotency_key=idempotency_key or uuid.uuid4().hex)

    def list_brands(self, limit: int = 50) -> list[dict]:
        return list(self._paged("/v1/brands", {"limit": limit}))

    def get_brand(self, brand_id: str, version: int | None = None) -> dict:
        return self.request("GET", f"/v1/brands/{brand_id}", params={"version": version} if version else None)

    def update_brand(self, brand_id: str, brand: dict, expected_version: int) -> dict:
        return self.request("PUT", f"/v1/brands/{brand_id}", json=brand, params={"expected_version": expected_version})

    def create_template(self, template: dict, *, idempotency_key: str | None = None) -> dict:
        return self.request("POST", "/v1/templates", json=template, idempotency_key=idempotency_key or uuid.uuid4().hex)

    def list_templates(self, limit: int = 50) -> list[dict]:
        return list(self._paged("/v1/templates", {"limit": limit}))

    def get_template(self, template_id: str, version: int | None = None) -> dict:
        return self.request("GET", f"/v1/templates/{template_id}", params={"version": version} if version else None)

    def update_template(self, template_id: str, template: dict, expected_version: int) -> dict:
        return self.request("PUT", f"/v1/templates/{template_id}", json=template, params={"expected_version": expected_version})

    # -- plans, videos, batches ----------------------------------------------------------------------------------------
    def create_plan(self, request: dict, *, idempotency_key: str | None = None) -> dict:
        return self.request("POST", "/v1/plans", json=request, idempotency_key=idempotency_key or uuid.uuid4().hex)

    def get_plan(self, plan_id: str) -> dict:
        return self.request("GET", f"/v1/plans/{plan_id}")

    def list_plans(self, limit: int = 50) -> list[dict]:
        return list(self._paged("/v1/plans", {"limit": limit}))

    def create_video(self, request: dict | None = None, *, plan_id: str | None = None, idempotency_key: str | None = None) -> dict:
        if bool(plan_id) == (request is not None):
            raise ValueError("provide exactly one of request or plan_id")
        return self.request("POST", "/v1/videos", json={"plan_id": plan_id} if plan_id else request,
                            idempotency_key=idempotency_key or uuid.uuid4().hex)

    def get_video(self, job_id: str) -> dict:
        return self.request("GET", f"/v1/videos/{job_id}")

    def list_videos(self, limit: int = 50, **filters) -> list[dict]:
        return list(self._paged("/v1/videos", {"limit": limit, **filters}))

    def cancel_video(self, job_id: str) -> dict:
        return self.request("POST", f"/v1/videos/{job_id}/cancel", idempotency_key=uuid.uuid4().hex)

    def delete_video(self, job_id: str) -> dict:
        return self.request("DELETE", f"/v1/videos/{job_id}")

    def resume_video(self, job_id: str, idempotency_key: str | None = None, **answer) -> dict:
        return self.request("POST", f"/v1/videos/{job_id}/resume", json=answer, idempotency_key=idempotency_key or uuid.uuid4().hex)

    def approve_budget(self, job_id: str, budget_usd: float | None = None, *, idempotency_key: str | None = None) -> dict:
        """Approve a `needs_input` job's approvable `budget_approval` stop (organizations billed by the operator): the server raises the
        limit by exactly what the stop needs. Prepaid (flat-priced) jobs never ask: their stop has `approvable: false`, is reviewed by
        Axiom Lab and answers 409 here. `budget_usd` is the legacy explicit total limit in USD and is only for old integrations."""
        answer = {"approve_budget": True} if budget_usd is None else {"budget_usd": budget_usd}
        return self.resume_video(job_id, idempotency_key, **answer)

    def confirm_duration(self, job_id: str, duration_sec: int, *, idempotency_key: str | None = None) -> dict:
        """Answer a `needs_input` job's `confirm_duration` stop: the materials were analysed and production starts with this film
        length in seconds, within the stop's `min_duration_sec`–`max_duration_sec` (its `suggested_duration_sec` unless changed)."""
        return self.resume_video(job_id, idempotency_key, duration_sec=duration_sec)

    def revise_video(self, job_id: str, changes: dict, *, idempotency_key: str | None = None) -> dict:
        return self.request("POST", f"/v1/videos/{job_id}/revisions", json=changes, idempotency_key=idempotency_key or uuid.uuid4().hex)

    def download_file(self, url: str) -> bytes:
        """Fetch a signed deliverable URL returned in `result.files` (no bearer header is needed or sent)."""
        response = self.transport.request("GET", url, timeout=self.timeout)
        if not 200 <= response.status_code < 300:
            raise VidgenError(response.status_code, None, response.headers)
        return response.content

    def create_batch(self, items: list[dict], *, name: str = "Batch", idempotency_key: str | None = None) -> dict:
        return self.request("POST", "/v1/batches", json={"name": name, "items": items}, idempotency_key=idempotency_key or uuid.uuid4().hex)

    def get_batch(self, batch_id: str) -> dict:
        return self.request("GET", f"/v1/batches/{batch_id}")

    def list_batches(self, limit: int = 50) -> list[dict]:
        return list(self._paged("/v1/batches", {"limit": limit}))

    def wait(self, job_id: str, *, timeout: float = 7200, interval: float = 10) -> dict:
        """Poll until a terminal status. `needs_input` is terminal for the caller: answer it, then wait again."""
        if timeout <= 0 or interval <= 0:
            raise ValueError("timeout and interval must be positive")
        deadline = time.monotonic() + timeout
        while True:
            job = self.get_video(job_id)
            if job["status"] in TERMINAL_STATUSES:
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"job {job_id} is still running; keep polling the same id")
            self._sleep(min(interval, remaining))

    # -- events, webhooks ------------------------------------------------------------------------------------------------
    def events(self, after: int = 0, limit: int = 50) -> Iterator[dict]:
        return self._paged("/v1/events", {"after": after, "limit": limit})

    def create_webhook(self, url: str, name: str = "Webhook", active: bool = True, *, idempotency_key: str | None = None) -> dict:
        return self.request("POST", "/v1/webhooks", json={"url": url, "name": name, "active": active},
                            idempotency_key=idempotency_key or uuid.uuid4().hex)

    def list_webhooks(self, limit: int = 50) -> list[dict]:
        return list(self._paged("/v1/webhooks", {"limit": limit}))

    def update_webhook(self, webhook_id: str, expected_version: int, **fields) -> dict:
        return self.request("PUT", f"/v1/webhooks/{webhook_id}", json=fields, params={"expected_version": expected_version})

    def rotate_webhook_secret(self, webhook_id: str, expected_version: int) -> dict:
        return self.request("POST", f"/v1/webhooks/{webhook_id}/rotate-secret", params={"expected_version": expected_version},
                            idempotency_key=uuid.uuid4().hex)

    def list_deliveries(self, limit: int = 50) -> list[dict]:
        return list(self._paged("/v1/webhook-deliveries", {"limit": limit}))

    def replay_delivery(self, delivery_id: str, webhook_id: str | None = None) -> dict:
        return self.request("POST", f"/v1/webhook-deliveries/{delivery_id}/replay", params={"webhook_id": webhook_id} if webhook_id else None,
                            idempotency_key=uuid.uuid4().hex)

    def checkout(self, amount_cents: int, *, idempotency_key: str | None = None) -> dict:
        """Start a top-up of `amount_cents` HKD cents (HK$100–HK$100,000); pay at the returned `url`, credited 1:1 once paid."""
        return self.request("POST", "/v1/wallet/checkout", json={"amount_cents": amount_cents}, idempotency_key=idempotency_key or uuid.uuid4().hex)

    def delete_brand(self, brand_id: str) -> dict:
        return self.request("DELETE", f"/v1/brands/{brand_id}")

    def delete_template(self, template_id: str) -> dict:
        return self.request("DELETE", f"/v1/templates/{template_id}")

    def delete_webhook(self, webhook_id: str) -> dict:
        return self.request("DELETE", f"/v1/webhooks/{webhook_id}")

    @staticmethod
    def verify_webhook(secret: str, signature_header: str, body: bytes, *, tolerance: int = 300, now: float | None = None) -> bool:
        """Verify `Vidgen-Signature: t=<unix>,v1=<hex>` on a received webhook body."""
        import hashlib
        import hmac
        try:
            fields = dict(part.split("=", 1) for part in signature_header.split(","))
            ts = int(fields["t"])
        except (ValueError, KeyError, AttributeError):
            return False
        if abs((time.time() if now is None else now) - ts) > tolerance:
            return False
        expected = hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(fields.get("v1", ""), expected)

    # -- lifecycle ----------------------------------------------------------------------------------------------------------
    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if close:
            close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
