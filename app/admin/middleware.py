"""Admin middleware: CSRF protection and security headers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.security.sessions import csrf_valid, ensure_csrf_cookie

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# The SPA is built by Vite into static files; no inline scripts are needed.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


class AdminSecurityMiddleware(BaseHTTPMiddleware):
    """Double-submit CSRF check on state-changing API calls, plus hardening headers."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.method in UNSAFE_METHODS and request.url.path.startswith("/api/") and not csrf_valid(request):
            response: Response = JSONResponse(
                {"detail": "Security token missing or expired. Reload the page and try again."}, status_code=403
            )
        else:
            response = await call_next(request)
        ensure_csrf_cookie(request, response)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response
