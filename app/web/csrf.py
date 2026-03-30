"""CSRF protection middleware.

Generates a per-session token and validates it on every state-changing request
(POST/PUT/PATCH/DELETE). The token is stored in the session and must be sent
as a form field ``csrf_token`` or header ``X-CSRF-Token``.

Exempt paths (API, webhooks, MCP) skip validation — they use their own auth.
"""

from __future__ import annotations

import secrets

from starlette.requests import Request
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

# Paths that use their own auth (Bearer token, webhook secret, etc.)
_EXEMPT_PREFIXES = ("/api/", "/mcp/", "/mcp", "/telegram/", "/health")
_EXEMPT_EXACT = {"/login", "/health", "/favicon.ico"}

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _is_exempt(path: str) -> bool:
    if path in _EXEMPT_EXACT:
        return True
    return any(path.startswith(p) for p in _EXEMPT_PREFIXES)


def get_csrf_token(request: Request) -> str:
    """Get or create a CSRF token for the current session."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


class CSRFMiddleware(BaseHTTPMiddleware):
    """Validate CSRF token on state-changing requests."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Safe methods and exempt paths skip CSRF check
        if request.method in _SAFE_METHODS or _is_exempt(request.url.path):
            return await call_next(request)

        # Unauthenticated users skip CSRF (they'll be redirected to login)
        if not request.session.get("authenticated"):
            return await call_next(request)

        session_token = request.session.get("csrf_token")
        if not session_token:
            return Response("CSRF token missing from session", status_code=403)

        # Check header first (HTMX sends X-CSRF-Token)
        submitted = request.headers.get("X-CSRF-Token")
        if not submitted:
            # Read raw body and extract csrf_token without consuming the stream
            # (BaseHTTPMiddleware creates a new request for call_next, so
            # parsing form here would lose data for downstream handlers)
            content_type = request.headers.get("content-type", "")
            if "application/x-www-form-urlencoded" in content_type:
                body = await request.body()
                from urllib.parse import parse_qs
                parsed = parse_qs(body.decode("utf-8", errors="replace"))
                submitted = parsed.get("csrf_token", [""])[0]
            elif "multipart/form-data" in content_type:
                # For multipart, read raw body and search for csrf_token field
                body = await request.body()
                body_str = body.decode("utf-8", errors="replace")
                import re
                match = re.search(r'name="csrf_token"\r?\n\r?\n([^\r\n-]+)', body_str)
                submitted = match.group(1).strip() if match else ""

        if not secrets.compare_digest(submitted or "", session_token):
            return Response("CSRF token invalid", status_code=403)

        return await call_next(request)
