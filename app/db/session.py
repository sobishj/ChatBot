"""SQLAlchemy engine creation and a lightweight database health check."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_engine() -> Engine:
    """Create the process-wide engine lazily, so importing this module never connects."""
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,  # transparently replace connections dropped by a DB restart
        pool_size=5,
        max_overflow=10,
        connect_args={"connect_timeout": 5},
    )


def check_database() -> dict[str, Any]:
    """Return ``{"ok": bool, ...}`` describing database reachability and pgvector availability.

    Error details are logged, never returned: this result is exposed on public endpoints.
    """
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            pgvector = (
                conn.execute(text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")).scalar()
                is not None
            )
        return {"ok": True, "pgvector_available": pgvector}
    except SQLAlchemyError as exc:
        logger.warning("Database health check failed: %s", exc)
        return {"ok": False, "error": exc.__class__.__name__}
