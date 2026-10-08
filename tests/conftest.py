"""Shared pytest fixtures.

Database tests run against a separate ``<name>_test`` database (created on demand),
never against the real application database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

# Defaults so unit tests can import the app outside Docker.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-123")


def _switch_to_test_database() -> str:
    """Point DATABASE_URL at ``<db>_test`` and create that database if it is missing."""
    url = make_url(os.environ["DATABASE_URL"])
    if url.database and url.database.endswith("_test"):
        return str(url.render_as_string(hide_password=False))
    test_url = url.set(database=f"{url.database}_test")
    try:
        admin = create_engine(url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
        with admin.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": test_url.database}).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{test_url.database}"'))
        admin.dispose()
    except Exception:  # noqa: BLE001 - no database available: DB tests will be skipped
        pass
    rendered = test_url.render_as_string(hide_password=False)
    os.environ["DATABASE_URL"] = rendered
    return rendered


_switch_to_test_database()

from app.config import get_settings  # noqa: E402
from app.db.session import check_database, new_session, reset_caches  # noqa: E402

_migrated = False


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Make each test see the current environment instead of a cached Settings/engine."""
    get_settings.cache_clear()
    reset_caches()
    yield
    get_settings.cache_clear()
    reset_caches()


@pytest.fixture
def live_db() -> None:
    """Skip the test unless the test database is reachable; migrate it once per run."""
    global _migrated
    if not check_database()["ok"]:
        pytest.skip("database not reachable")
    if not _migrated:
        from app.db.migrate import run_migrations

        run_migrations()
        _migrated = True


ALL_TABLES = (
    "rate_limits, jobs, questions, chunks, documents, pages, user_clients, clients, ai_models, users, settings"
)


@pytest.fixture
def db(live_db: None) -> Iterator[Session]:
    """A session on a clean test database (all tables truncated after the test)."""
    session = new_session()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text(f"TRUNCATE {ALL_TABLES} RESTART IDENTITY CASCADE"))
        session.commit()
        session.close()
