"""Request gateway: Host/Origin validation, MCP bearer auth, browser sessions + CSRF.

The gateway wraps both the FastAPI app and the MCP Streamable HTTP app:

* Every request must carry an allowed loopback Host header (DNS-rebinding defence).
* A present Origin header must be one of this server's loopback origins.
* ``/mcp`` requires ``Authorization: Bearer <token>``; the token is never logged
  and never reaches browser JavaScript.
* ``/api`` requires the browser session cookie, and mutating methods also
  require the ``X-CSRF-Token`` header bound to that session.
"""

from __future__ import annotations

import hmac
import json
import secrets
from http.cookies import SimpleCookie

from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings

SESSION_COOKIE = "sw_session"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_SESSIONS = 256


class SessionStore:
    """In-memory sessions for the single local user. Restarting the server
    invalidates them; the browser simply reloads to get a new one."""

    def __init__(self) -> None:
        self._sessions: dict[str, str] = {}

    def create(self) -> tuple[str, str]:
        if len(self._sessions) >= MAX_SESSIONS:
            self._sessions.pop(next(iter(self._sessions)))
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._sessions[sid] = csrf
        return sid, csrf

    def csrf_for(self, sid: str | None) -> str | None:
        return self._sessions.get(sid) if sid else None


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def cookie_value(headers: dict[str, str], name: str) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # noqa: BLE001
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


async def _reject(
    send: Send, status: int, code: str, message: str, extra: list[tuple[bytes, bytes]] | None = None
) -> None:
    body = json.dumps({"error": {"code": code, "message": message, "details": {}}}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers + (extra or [])})
    await send({"type": "http.response.body", "body": body})


class Gateway:
    def __init__(self, api: ASGIApp, mcp: ASGIApp, settings: Settings, token: str, sessions: SessionStore):
        self.api = api
        self.mcp = mcp
        self.settings = settings
        self._token = token.encode()
        self.sessions = sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.api(scope, receive, send)
            return
        headers = _headers(scope)
        if headers.get("host", "") not in self.settings.allowed_hosts:
            await _reject(send, 421, "invalid_host", "Requests must address this server via its loopback address.")
            return
        origin = headers.get("origin")
        if origin is not None and origin not in self.settings.allowed_origins:
            await _reject(send, 403, "invalid_origin", "Cross-origin requests are not allowed.")
            return
        path: str = scope["path"]
        if path == "/mcp" or path.startswith("/mcp/"):
            if not self._bearer_ok(headers.get("authorization", "")):
                await _reject(
                    send,
                    401,
                    "unauthorized",
                    "A valid bearer token is required for MCP.",
                    [(b"www-authenticate", b'Bearer realm="sculpt-workspace"')],
                )
                return
            if path != "/mcp":
                scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}
            await self.mcp(scope, receive, send)
            return
        if path.startswith("/api/") and path != "/api/health":
            sid = cookie_value(headers, SESSION_COOKIE)
            csrf = self.sessions.csrf_for(sid)
            if csrf is None:
                await _reject(send, 401, "session_required", "Your browser session expired. Reload the page.")
                return
            if scope["method"] not in SAFE_METHODS and not hmac.compare_digest(
                headers.get(CSRF_HEADER, "").encode(), csrf.encode()
            ):
                await _reject(send, 403, "csrf_failed", "The request is missing a valid CSRF token. Reload the page.")
                return
        await self.api(scope, receive, send)

    def _bearer_ok(self, header: str) -> bool:
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value:
            return False
        return hmac.compare_digest(value.strip().encode(), self._token)


def session_cookie_header(sid: str) -> str:
    return f"{SESSION_COOKIE}={sid}; HttpOnly; SameSite=Strict; Path=/"
