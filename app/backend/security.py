"""
Request-level protections applied to every route (pure ASGI, so streamed responses pass through untouched).

- Request body limits (SEC-004): a declared Content-Length over the route's limit is refused with 413 before the
  handler runs; a body without one (chunked) is counted while it is read and refused as soon as it passes the
  limit. Limits: PHRONESIS_MAX_BODY_MB (default 2 MB) for JSON routes, PHRONESIS_MAX_EXPORT_BODY_MB (default 16 MB)
  for report export and client-manifest re-runs, which carry plots and manifests. The multipart dataset upload
  (POST /api/datasets) has its own streamed, per-kind limits and is not counted here.
- Origin check on state-changing methods (SEC-002): a browser request whose Origin is not in the CORS allow-list
  is refused with 403. Browsers attach Origin to every cross-origin POST, including the "simple" text/plain and
  multipart requests that skip the CORS preflight, so a third-party page cannot submit jobs or uploads.
- Host allow-list (SEC-002, DNS rebinding): when authentication is disabled, or PHRONESIS_ALLOWED_HOSTS is set, a
  request whose Host header is not allowed is refused with 400. With auth disabled the default list is the
  loopback names (localhost, 127.0.0.1, [::1]); a rebound attacker hostname never matches.
- Loopback-only clients when authentication is disabled (SEC-002 / CFG-001): every request then runs as one
  local user, so a client that is not on the loopback interface is refused with 403 unless the operator sets
  PHRONESIS_ALLOW_INSECURE=1 (e.g. inside a container whose port is published on 127.0.0.1 only).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from starlette.exceptions import HTTPException

logger = logging.getLogger("phronesis.backend.security")

Scope = Dict[str, Any]
Message = Dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

DEFAULT_CORS_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000"
_STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")
_LARGE_BODY_PREFIXES = ("/api/export/", "/api/runs/rerun")
_UNCOUNTED = (("POST", "/api/datasets"),)  # multipart upload: streamed with its own limits in the datasets router


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _mb(name: str, default: float) -> int:
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError:
        value = default
    return int(max(0.01, value) * 1024 * 1024)


def max_body_bytes(method: str, path: str) -> Optional[int]:
    if (method, path.rstrip("/")) in _UNCOUNTED:
        return None
    if any(path.startswith(p) for p in _LARGE_BODY_PREFIXES):
        return _mb("PHRONESIS_MAX_EXPORT_BODY_MB", 16.0)
    return _mb("PHRONESIS_MAX_BODY_MB", 2.0)


def cors_origins() -> List[str]:
    """PHRONESIS_CORS_ORIGINS without wildcards: '*' together with credentials would let any page read responses."""
    raw = os.getenv("PHRONESIS_CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    out = []
    for item in raw.split(","):
        origin = item.strip().rstrip("/")
        if not origin:
            continue
        if "*" in origin:
            logger.warning("Ignoring wildcard CORS origin %r: origins must be listed explicitly.", origin)
            continue
        out.append(origin)
    return out


def allowed_origins() -> List[str]:
    extra = [o.strip().rstrip("/") for o in os.getenv("PHRONESIS_MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return list(dict.fromkeys(cors_origins() + [o for o in extra if "*" not in o]))


def _auth_disabled() -> bool:
    from app.backend.auth import auth_disabled

    return auth_disabled()


def allowed_hosts() -> Optional[List[str]]:
    """Host names requests may address, or None when no Host check applies (bearer auth and no explicit list)."""
    raw = [h.strip().lower() for h in os.getenv("PHRONESIS_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if raw:
        return raw
    if not _auth_disabled():
        return None
    hosts = list(_LOOPBACK_HOSTS)
    if "PYTEST_CURRENT_TEST" in os.environ:
        hosts.append("testserver")  # FastAPI TestClient's default Host under pytest only
    return hosts


def _host_name(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8000
        end = host.find("]")
        return host[: end + 1] if end > 0 else host
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host


def is_loopback_client(client: Optional[Iterable[Any]]) -> bool:
    if not client:
        return False
    host = str(list(client)[0])
    if host == "testclient" and "PYTEST_CURRENT_TEST" in os.environ:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def insecure_allowed() -> bool:
    return _truthy(os.getenv("PHRONESIS_ALLOW_INSECURE"))


def _headers(scope: Scope) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in scope.get("headers") or []:
        try:
            out[key.decode("latin-1").lower()] = value.decode("latin-1")
        except Exception:  # noqa: BLE001
            continue
    return out


class _BodyTooLarge(HTTPException):
    """Raised from receive(): an HTTPException, so FastAPI's body parsing re-raises it and the handler answers 413."""

    def __init__(self, limit: int) -> None:
        super().__init__(status_code=413, detail=f"Request body too large (limit {limit // 1024} KiB).")


async def _send_json(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


class SecurityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method") or "GET").upper()
        path = str(scope.get("path") or "")
        headers = _headers(scope)

        if _auth_disabled() and not insecure_allowed() and not is_loopback_client(scope.get("client")):
            await _send_json(
                send, 403,
                "Authentication is disabled, so this server only answers clients on this machine. Configure "
                "PHRONESIS_API_TOKENS or PHRONESIS_JWT_SECRET to serve other hosts.",
            )
            return

        hosts = allowed_hosts()
        if hosts is not None and _host_name(headers.get("host", "")) not in hosts:
            await _send_json(send, 400, "Host not allowed.")
            return

        origin = headers.get("origin")
        if method in _STATE_CHANGING and origin is not None and origin.rstrip("/") not in allowed_origins():
            await _send_json(send, 403, "Origin not allowed.")
            return

        limit = max_body_bytes(method, path)
        if limit is None:
            await self.app(scope, receive, send)
            return
        declared = headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            await _send_json(send, 413, f"Request body too large (limit {limit // 1024} KiB).")
            return

        received = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    raise _BodyTooLarge(limit)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await _send_json(send, 413, f"Request body too large (limit {limit // 1024} KiB).")
