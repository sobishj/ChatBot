"""FastAPI application for the public side (APP_PORT): chat API, client config, widget.js."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api.health import router as health_router
from app.api.public import cors_headers, request_origin
from app.api.public import router as public_router


def create_public_app() -> FastAPI:
    """Build the public app. No admin routes are ever registered here."""
    app = FastAPI(
        title="Website Assistant - Public API",
        version=__version__,
        docs_url=None,  # no interactive docs on the internet-facing port
        redoc_url=None,
        openapi_url=None,
    )

    async def _error(request: Request, status: int, detail: object) -> JSONResponse:
        # Errors from allowed widget pages still need CORS headers so the widget can read them.
        headers: dict[str, str] = {}
        origin = request_origin(request)
        if origin and request.url.path.startswith("/api/") and status != 403:
            from app.api.public import _all_allowed_domains
            from app.services.clients import origin_allowed

            if origin_allowed(origin, _all_allowed_domains()):
                headers = cors_headers(origin)
        return JSONResponse({"detail": detail}, status_code=status, headers=headers)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return await _error(request, exc.status_code, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, _exc: RequestValidationError) -> JSONResponse:
        return await _error(request, 422, "Invalid request.")

    app.include_router(health_router)
    app.include_router(public_router)
    return app
