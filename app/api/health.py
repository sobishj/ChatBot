"""``GET /health``, mounted on both the admin and the public server."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from app import __version__
from app.db.session import check_database

router = APIRouter(tags=["health"])


@router.get("/health")
def health(response: Response) -> dict[str, Any]:
    """Report service health. Returns 503 when a required dependency is down."""
    database = check_database()
    healthy = database["ok"]
    if not healthy:
        response.status_code = 503
    return {
        "status": "ok" if healthy else "unavailable",
        "version": __version__,
        "checks": {"database": database},
    }
