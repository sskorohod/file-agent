"""Authentication middleware for the web dashboard."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

EXEMPT_PREFIXES = ("/health", "/api/v1/", "/login")


class AuthMiddleware(BaseHTTPMiddleware):
    """Check session for authentication; redirect to /login if not authenticated."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Skip auth for exempt paths
        if any(path == p or path.startswith(p + "/") or path.startswith(p) for p in EXEMPT_PREFIXES):
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
