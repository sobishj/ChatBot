"""FastAPI application for the admin side (ADMIN_PORT): JSON API + React UI."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app import __version__
from app.admin.middleware import AdminSecurityMiddleware
from app.admin.routes import auth, clients, dashboard, jobs, models, settings, setup, system, users
from app.admin.spa import mount_spa
from app.api.health import router as health_router
from app.api.public import widget_js as public_widget_js
from app.documents.service import DocumentError
from app.services.ai_models import ModelError
from app.services.clients import ClientError
from app.services.users import UserError


def create_admin_app() -> FastAPI:
    app = FastAPI(
        title="Website Assistant - Admin",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(AdminSecurityMiddleware)

    @app.exception_handler(ClientError)
    @app.exception_handler(ModelError)
    @app.exception_handler(UserError)
    @app.exception_handler(DocumentError)
    async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    app.include_router(health_router)
    for module in (auth, setup, dashboard, clients, models, settings, jobs, users, system):
        app.include_router(module.router)

    @app.get("/widget.js", include_in_schema=False)
    def widget_js() -> FileResponse:
        """Same widget file as the public port, for the Branding tab's live preview."""
        return public_widget_js()

    mount_spa(app)  # must be last: catch-all route for the React app
    return app
