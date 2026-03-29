"""Authentication middleware for the web dashboard."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

# Exact paths that don't require auth
_EXEMPT_EXACT = {"/health", "/login", "/telegram/webhook", "/favicon.ico"}
# Prefixes that don't require auth (must end with /)
_EXEMPT_PREFIXES = ("/api/v1/", "/mcp/", "/mcp", "/static/")


def _is_exempt(path: str) -> bool:
    if path in _EXEMPT_EXACT:
        return True
    return any(path.startswith(p) for p in _EXEMPT_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    """Check session for authentication; redirect to /login if not authenticated."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Skip auth for exempt paths
        if _is_exempt(path):
            return await call_next(request)

        if not request.session.get("authenticated"):
            # HTMX requests get a special header so the browser redirects
            if request.headers.get("HX-Request"):
                return Response(
                    status_code=401,
                    headers={"HX-Redirect": "/login"},
                )
            return RedirectResponse("/login", status_code=303)

        return await call_next(request)
