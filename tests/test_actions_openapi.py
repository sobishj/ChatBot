"""Importing actions from a client's OpenAPI / Swagger spec."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.actions import executor
from app.actions.openapi import SpecError, endpoints, parse_text, suggest_name
from app.services.actions import update_settings
from app.services.settings import set_setting
from demo import mock_booking_api

SWAGGER_2 = {
    "swagger": "2.0",
    "info": {"title": "Clinic", "version": "2"},
    "basePath": "/v1",
    "definitions": {
        "Booking": {
            "type": "object",
            "required": ["doctorId", "mobile", "services"],
            "properties": {
                "doctorId": {"type": "integer", "description": "Doctor"},
                "mobile": {"type": "string"},
                "services": {"type": "array", "items": {"type": "string"}},
                "createdAt": {"type": "string", "readOnly": True},
            },
        }
    },
    "paths": {
        "/slots": {
            "get": {
                "operationId": "getSlots",
                "summary": "Free slots for a doctor on a day",
                "parameters": [
                    {"name": "doctorId", "in": "query", "type": "integer", "required": True},
                    {"name": "shift", "in": "query", "type": "string", "enum": ["morning", "evening"]},
                    {"name": "X-Clinic", "in": "header", "type": "string"},
                ],
            }
        },
        "/bookings": {"post": {"operationId": "createBooking", "parameters": [{"name": "body", "in": "body", "required": True, "schema": {"$ref": "#/definitions/Booking"}}]}},
        "/bookings/{bookingId}": {
            "parameters": [{"name": "bookingId", "in": "path", "required": True, "type": "string"}],
            "delete": {"operationId": "cancelBooking", "deprecated": True},
        },
    },
}


def test_mock_api_spec_becomes_ready_to_use_actions() -> None:
    found = {e["key"]: e for e in endpoints(mock_booking_api.app.openapi(), "X-API-Key")}
    assert set(found) == {"GET /health", "GET /departments", "GET /doctors", "GET /slots", "POST /appointments", "DELETE /appointments/{appointment_id}"}
    assert all(e["importable"] and not e["notes"] for e in found.values())  # the auth header is skipped silently

    book = found["POST /appointments"]["action"]
    assert book["name"] == "create_appointments" and book["requires_confirmation"]
    assert {p["name"]: (p["type"], p["location"], p["required"]) for p in book["parameters"]} == {
        "doctor_id": ("integer", "body", True),
        "date": ("date", "body", True),
        "time": ("string", "body", True),
        "patient_name": ("string", "body", True),
        "phone": ("phone", "body", True),
    }
    assert "POST /appointments" in book["description"]  # short summaries get the endpoint added
    slots = found["GET /slots"]["action"]
    assert slots["name"] == "get_slots" and not slots["requires_confirmation"]
    assert {p["name"]: p["type"] for p in slots["parameters"]} == {"doctor_id": "integer", "date": "date"}
    cancel = found["DELETE /appointments/{appointment_id}"]["action"]
    assert cancel["name"] == "delete_appointments_by_appointment_id" and cancel["parameters"][0]["location"] == "path"


def test_swagger_2_with_unsupported_parts() -> None:
    found = {e["key"]: e for e in endpoints(SWAGGER_2, "X-API-Key", "https://api.clinic.test")}
    slots = found["GET /v1/slots"]  # basePath is added when the API URL doesn't include it
    assert slots["action"]["name"] == "get_slots" and slots["importable"]
    shift = next(p for p in slots["action"]["parameters"] if p["name"] == "shift")
    assert shift["enum"] == ["morning", "evening"] and not shift["required"]
    assert any("header parameter X-Clinic" in n for n in slots["notes"])

    booking = found["POST /v1/bookings"]
    assert not booking["importable"] and any("services" in n for n in booking["notes"])  # required list field
    assert {p["name"]: p["type"] for p in booking["action"]["parameters"]} == {"doctorId": "integer", "mobile": "phone"}

    cancel = found["DELETE /v1/bookings/{bookingId}"]
    assert cancel["importable"] and cancel["action"]["name"] == "cancel_booking" and "deprecated in the spec" in cancel["notes"]
    assert endpoints(SWAGGER_2, "X-API-Key", "https://api.clinic.test/v1")[0]["path"] == "/slots"  # already in the API URL


def test_parse_text_accepts_yaml_and_rejects_other_files() -> None:
    assert parse_text("openapi: 3.0.0\ninfo: {title: T}\npaths:\n  /a:\n    get: {summary: A}\n")["paths"]
    with pytest.raises(SpecError):
        parse_text('{"hello": "world"}')
    with pytest.raises(SpecError):
        parse_text("::: not yaml :::\n\t-")
    assert suggest_name("GET", "/doctors/{doctorId}/slots", "doctor_slots_doctors__doctor_id__slots_get") == "get_doctors_by_doctor_id_slots"


# ----------------------------------------------------------------------------- admin API
@pytest.fixture
def hospital(db: Session, embedder, make_client, ai_model, monkeypatch: pytest.MonkeyPatch):
    seen: list[httpx.Request] = []
    mock = TestClient(mock_booking_api.app, base_url="http://host.docker.internal:8100")

    class Recorder(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.host == "specs.example.com":
                return httpx.Response(200, json=mock_booking_api.app.openapi())
            return mock._transport.handle_request(request)

    monkeypatch.setattr(executor, "http_client", lambda timeout: httpx.Client(transport=Recorder(), follow_redirects=False))
    monkeypatch.setattr(executor, "resolve_host", lambda host: {"10.0.0.8"} if host == "internal.example.com" else {"93.184.216.34"})
    set_setting(db, "mode", "onprem")
    client = make_client("hospital", "Demo Hospital")
    update_settings(client, {"enabled": True, "base_url": "http://host.docker.internal:8100", "auth_name": "X-API-Key", "api_key": "demo-key"}, allow_private=True)
    db.commit()
    return client, seen


def _root(db: Session, monkeypatch: pytest.MonkeyPatch):
    from tests.test_api import admin_client, finish_setup

    root = admin_client()
    finish_setup(db, root, monkeypatch)
    set_setting(db, "mode", "onprem")  # finish_setup chose cloud mode; the demo API is on a private address
    db.commit()
    return root


def test_discover_and_import_from_the_api(db: Session, hospital, monkeypatch: pytest.MonkeyPatch) -> None:
    _client, seen = hospital
    root = _root(db, monkeypatch)
    url = "/api/admin/clients/hospital/api-actions"
    found = root.post(f"{url}/openapi/discover", json={}).json()
    assert found["source"] == "http://host.docker.internal:8100/openapi.json" and found["title"] == "Mock booking API"
    assert seen[-1].headers["X-API-Key"] == "demo-key"  # the spec is fetched with the client's own auth
    drafts = {e["key"]: e["action"] for e in found["endpoints"]}

    chosen = [drafts["GET /slots"], drafts["POST /appointments"]]
    imported = root.post(f"{url}/openapi/import", json={"actions": chosen}).json()
    assert imported["added"] == ["get_slots", "create_appointments"] and imported["errors"] == []
    names = {a["name"]: a for a in imported["actions"]}
    assert names["create_appointments"]["requires_confirmation"] and len(names["create_appointments"]["parameters"]) == 5

    again = root.post(f"{url}/openapi/discover", json={}).json()  # remembered URL, and already-added endpoints marked
    assert {e["key"] for e in again["endpoints"] if e["added"]} == {"GET /slots", "POST /appointments"}
    assert root.get(url).json()["settings"]["openapi_url"].endswith("/openapi.json")
    duplicate = root.post(f"{url}/openapi/import", json={"actions": [drafts["GET /slots"], drafts["GET /doctors"]]}).json()
    assert duplicate["added"] == ["get_doctors"] and "already exists" in duplicate["errors"][0]

    # The imported action really works against the API.
    slots = names["get_slots"]
    ran = root.post(f"{url}/actions/{slots['id']}/test", json={"params": {"doctor_id": 1, "date": "2026-10-12"}}).json()
    assert ran["ok"] and "09:30" in ran["response"]


def test_spec_from_other_host_or_upload_never_gets_the_key(db: Session, hospital, monkeypatch: pytest.MonkeyPatch) -> None:
    _client, seen = hospital
    root = _root(db, monkeypatch)
    url = "/api/admin/clients/hospital/api-actions"
    other = root.post(f"{url}/openapi/discover", json={"spec_url": "https://specs.example.com/booking.json"}).json()
    assert len(other["endpoints"]) == 6 and "X-API-Key" not in seen[-1].headers

    uploaded = root.post(f"{url}/openapi/discover", json={"spec_text": json.dumps(SWAGGER_2)}).json()
    assert uploaded["source"] == "uploaded file" and len(uploaded["endpoints"]) == 3

    assert root.post(f"{url}/openapi/discover", json={"spec_text": "not a spec"}).status_code == 400


def test_spec_url_is_ssrf_checked_and_super_admin_only(db: Session, hospital, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_api import admin_client

    root = _root(db, monkeypatch)
    url = "/api/admin/clients/hospital/api-actions"
    set_setting(db, "mode", "cloud")
    db.commit()
    blocked = root.post(f"{url}/openapi/discover", json={"spec_url": "https://internal.example.com/openapi.json"})
    assert blocked.status_code == 400 and "private" in blocked.json()["detail"]

    client_id = root.get("/api/admin/clients/hospital").json()["client"]["id"]
    root.post("/api/admin/users", json={"name": "Ann", "email": "ann@example.com", "password": "ann-password-1", "role": "client_admin", "client_ids": [client_id]})
    ann = admin_client()
    ann.post("/api/admin/auth/login", json={"email": "ann@example.com", "password": "ann-password-1"})
    assert ann.post(f"{url}/openapi/discover", json={}).status_code == 403
    assert ann.post(f"{url}/openapi/import", json={"actions": []}).status_code == 403
