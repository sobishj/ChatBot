"""FastAPI application for the public side (APP_PORT): chat API, client config, widget.js."""

from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.api.health import router as health_router


def create_public_app() -> FastAPI:
    """Build the public app. No admin routes are ever registered here."""
    app = FastAPI(
        title="Website Assistant - Public API",
        version=__version__,
        docs_url=None,  # no interactive docs on the internet-facing port
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(health_router)
    return app
