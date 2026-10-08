"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Defaults so unit tests can import the app outside Docker. Inside the container the
# real values from .env take precedence (setdefault never overwrites).
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-123")

from app.config import get_settings  # noqa: E402
from app.db.session import check_database, get_engine  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Make each test see the current environment instead of a cached Settings."""
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()


@pytest.fixture
def live_db() -> None:
    """Skip the test unless the real database is reachable (e.g. inside the app container)."""
    if not check_database()["ok"]:
        pytest.skip("database not reachable")
