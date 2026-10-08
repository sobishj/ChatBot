"""FastAPI application for the admin UI (ADMIN_PORT)."""

from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.api.health import router as health_router


def create_admin_app() -> FastAPI:
    """Build the admin app. Setup wizard and admin pages are added in later steps."""
    app = FastAPI(
        title="Website Assistant - Admin",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(health_router)
    return app
