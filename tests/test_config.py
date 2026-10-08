"""Tests for bootstrap settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("APP_PORT", "ADMIN_PORT", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/x")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.app_port == 8000
    assert settings.admin_port == 8001
    assert str(settings.data_dir).replace("\\", "/") == "/data"


def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/x")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("APP_PORT", "9000")
    monkeypatch.setenv("ADMIN_PORT", "9001")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert (settings.app_port, settings.admin_port) == (9000, 9001)


def test_short_secret_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/x")
    monkeypatch.setenv("SECRET_KEY", "too-short")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_missing_database_url_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SECRET_KEY", "x" * 40)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
