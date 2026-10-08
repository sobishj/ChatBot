"""Tests for the /health endpoint on both servers."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import __version__
from app.admin_app import create_admin_app
from app.api import health as health_module
from app.public_app import create_public_app

APP_FACTORIES = [create_admin_app, create_public_app]


@pytest.mark.parametrize("factory", APP_FACTORIES)
def test_health_ok(factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_module, "check_database", lambda: {"ok": True, "pgvector_available": True})
    client = TestClient(factory())

    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["checks"]["database"]["ok"] is True


@pytest.mark.parametrize("factory", APP_FACTORIES)
def test_health_database_down_returns_503(factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_module, "check_database", lambda: {"ok": False, "error": "OperationalError"})
    client = TestClient(factory())

    resp = client.get("/health")

    assert resp.status_code == 503
    assert resp.json()["status"] == "unavailable"


def test_unreachable_database_does_not_leak_details(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real failed connection reports only the error class, never host/password."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://secretuser:secretpw@127.0.0.1:1/nodb")
    client = TestClient(create_public_app())

    resp = client.get("/health")

    assert resp.status_code == 503
    assert "secret" not in resp.text


@pytest.mark.parametrize("factory", APP_FACTORIES)
def test_no_api_docs_exposed(factory: Any) -> None:
    app: FastAPI = factory()
    client = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


@pytest.mark.integration
def test_health_against_real_database(live_db: None) -> None:
    client = TestClient(create_admin_app())

    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["checks"]["database"]["pgvector_available"] is True
