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

        # Check header first (HTMX), then form field
        submitted = request.headers.get("X-CSRF-Token")
        if not submitted:
            # Parse form body to get csrf_token field
            content_type = request.headers.get("content-type", "")
            if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
                form = await request.form()
                submitted = form.get("csrf_token")
                # Re-create receive so downstream handlers can read the body again
                # Starlette caches form data internally, so this works transparently

        if not secrets.compare_digest(submitted or "", session_token):
            return Response("CSRF token invalid", status_code=403)

        return await call_next(request)
