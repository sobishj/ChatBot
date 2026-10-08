"""Serve the built React app: static assets plus index.html for client-side routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

DIST_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"

_MISSING_BUILD = """<!doctype html><meta charset="utf-8"><title>Website Assistant</title>
<p style="font-family:sans-serif;padding:2rem">The admin UI has not been built.
Run <code>npm run build</code> in <code>frontend/</code> (Docker builds it automatically).</p>"""


def mount_spa(app: FastAPI) -> None:
    """Mount /assets and a catch-all GET route returning index.html (must be registered last)."""
    assets = DIST_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    def spa(path: str) -> FileResponse | HTMLResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found.")
        candidate = (DIST_DIR / path).resolve()
        # Top-level static files from Vite's public/ folder (favicon etc.).
        if path and candidate.is_file() and candidate.parent == DIST_DIR:
            return FileResponse(candidate)
        index = DIST_DIR / "index.html"
        if not index.is_file():
            return HTMLResponse(_MISSING_BUILD, status_code=503)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
