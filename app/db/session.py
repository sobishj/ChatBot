"""SQLAlchemy engine/session management and a lightweight database health check."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_engine() -> Engine:
    """Create the process-wide engine lazily, so importing this module never connects."""
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,  # transparently replace connections dropped by a DB restart
        pool_size=10,
        max_overflow=20,
        connect_args={"connect_timeout": 5},
    )


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def new_session() -> Session:
    """Return a new ORM session (caller must close it)."""
    return _session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error, always close."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session; routes commit explicitly."""
    session = new_session()
    try:
        yield session
    finally:
        session.close()


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


def reset_caches() -> None:
    """Forget the cached engine/session factory (tests that change DATABASE_URL)."""
    _session_factory.cache_clear()
    get_engine.cache_clear()
