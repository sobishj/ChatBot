"""API actions: tool loop, confirmations, validation, SSRF, secrecy, permissions, rate limits, audit."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.actions import executor
from app.actions.executor import ActionError, check_target, validate_params
from app.actions.flow import CANCELLED_ANSWER, EXPIRED_ANSWER, LIMIT_ANSWER, MAX_TOOL_ROUNDS
from app.chat import service as chat_service
from app.db.models import ActionCall, ActionSession, ClientAction, Question
from app.public_app import create_public_app
from app.services.actions import add_template, update_settings
from app.services.settings import set_setting, update_setting_dict
from tests.fakes import FakeLLM, FakeToolLLM

API_KEY = "sk-live-secret-1234"
BASE = "https://booking.test/api"
ORIGIN = "https://www.clinic.test"
PHONE = "9876543210"


class BookingAPI:
    """In-memory stand-in for the client's booking API (served through httpx.MockTransport)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.next_id = 1001

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("X-API-Key") != API_KEY:
            return httpx.Response(401, json={"detail": "bad key"})
        path = request.url.path.removeprefix("/api")
        if request.method == "GET" and path == "/slots":
            return httpx.Response(200, json={"doctor_id": int(request.url.params["doctor_id"]), "date": request.url.params["date"], "free": ["09:30", "11:00"]})
        if request.method == "POST" and path == "/appointments":
            body = json.loads(request.content)
            booking = f"B-{self.next_id}"
            self.next_id += 1
            return httpx.Response(201, json={"booking_id": booking, "patient_name": body["patient_name"]})
        if request.method == "DELETE" and path.startswith("/appointments/"):
            return httpx.Response(200, json={"cancelled": path.rsplit("/", 1)[1]})
        return httpx.Response(404, json={"detail": "not found"})

    def made(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method]


@pytest.fixture
def booking_api(monkeypatch: pytest.MonkeyPatch) -> BookingAPI:
    api = BookingAPI()
    monkeypatch.setattr(executor, "http_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(api), follow_redirects=False))
    monkeypatch.setattr(executor, "resolve_host", lambda host: {"93.184.216.34"})  # a public address
    return api


@pytest.fixture
def clinic(db: Session, embedder, make_client, ai_model, booking_api):
    client = make_client("clinic", "City Clinic", ORIGIN, ["clinic.test"])
    update_settings(client, {"enabled": True, "base_url": BASE, "auth_type": "header", "auth_name": "X-API-Key", "api_key": API_KEY}, allow_private=False)
    add_template(db, client)
    db.commit()
    return client


def ask(db: Session, client, message: str, tool_llm: FakeToolLLM, llm: FakeLLM | None = None, session: str = "visitor-session-1", confirm: bool | None = None):
    return chat_service.answer_question(db, client, session, message, embedder=None, confirm=confirm, tool_llm=tool_llm, llm=llm or FakeLLM())


BOOKING_ARGS = {"doctor_id": 3, "date": "2026-10-10", "time": "09:30", "patient_name": "Sobish", "phone": "[phone_1]"}


# ----------------------------------------------------------------------------- off by default
def test_disabled_feature_answers_exactly_as_before(db: Session, clinic, booking_api: BookingAPI) -> None:
    clinic.api_actions_enabled = False
    db.commit()
    tool_llm, llm = FakeToolLLM(), FakeLLM(reply="We open at 9.")
    result = ask(db, clinic, "When do you open? Call me on 9876543210", tool_llm, llm)
    assert result.answer == "We open at 9." and not tool_llm.calls and len(llm.calls) == 1
    assert result.confirmation is None and result.action_results == []
    system = llm.calls[0][1][0]["content"]
    assert "LIVE ACTIONS" not in system and "[phone]" in llm.calls[0][1][-1]["content"]  # unchanged masking
    assert not booking_api.requests
    assert db.scalar(select(Question.actions_used)) == []


def test_new_clients_have_actions_off(db: Session, make_client) -> None:
    assert make_client("fresh", "Fresh").api_actions_enabled is False


# ----------------------------------------------------------------------------- GET actions
def test_get_action_runs_and_answer_uses_result(db: Session, clinic, booking_api: BookingAPI) -> None:
    tool_llm = FakeToolLLM([[("get_slots", {"doctor_id": 3, "date": "2026-10-10"})], "Dr. Anil is free at 09:30 and 11:00."])
    result = ask(db, clinic, "Is Dr. Anil free on 10 October?", tool_llm)
    assert result.answer == "Dr. Anil is free at 09:30 and 11:00." and result.answered
    assert result.actions_used == ["get_slots"] and result.action_results[0]["ok"]
    sent = booking_api.made("GET")[0]
    assert sent.headers["X-API-Key"] == API_KEY and sent.url.params["date"] == "2026-10-10"
    tool_message = tool_llm.calls[1]["messages"][-1]
    assert tool_message["role"] == "tool" and "UNTRUSTED TOOL OUTPUT" in tool_message["content"] and "09:30" in tool_message["content"]
    assert "LIVE ACTIONS" in tool_llm.calls[0]["messages"][0]["content"]
    assert {t["function"]["name"] for t in tool_llm.calls[0]["tools"]} >= {"get_slots", "book_appointment"}
    question = db.scalar(select(Question))
    assert question.actions_used == ["get_slots"] and question.input_tokens == 100  # both rounds counted


# ----------------------------------------------------------------------------- confirmation
def test_booking_needs_confirmation_then_runs_on_yes(db: Session, clinic, booking_api: BookingAPI) -> None:
    tool_llm = FakeToolLLM([[("book_appointment", BOOKING_ARGS)]])
    first = ask(db, clinic, f"Book Dr. Anil on 2026-10-10 at 09:30 for Sobish, phone {PHONE}", tool_llm)
    assert first.confirmation and first.confirmation["action"] == "book_appointment"
    assert "Please confirm" in first.answer and "98xxxxxx10" in first.answer and PHONE not in first.answer
    assert not booking_api.made("POST")  # nothing booked yet
    assert PHONE not in json.dumps(tool_llm.calls[0]["messages"]) and "[phone_1]" in tool_llm.calls[0]["messages"][-1]["content"]

    tool_llm.script = ["Booked! Your booking ID is B-1001."]
    second = ask(db, clinic, "Yes", tool_llm)
    posted = booking_api.made("POST")
    assert len(posted) == 1 and json.loads(posted[0].content)["phone"] == PHONE  # real value only to the client's API
    assert "B-1001" in second.answer and second.actions_used == ["book_appointment"]
    assert "B-1001" in tool_llm.calls[-1]["messages"][-1]["content"]

    # A second "yes" does nothing: the pending action was used up.
    tool_llm.script = ["Is there anything else?"]
    ask(db, clinic, "yes", tool_llm)
    assert len(booking_api.made("POST")) == 1


def test_confirm_buttons_and_cancel(db: Session, clinic, booking_api: BookingAPI) -> None:
    tool_llm = FakeToolLLM([[("book_appointment", BOOKING_ARGS)]])
    ask(db, clinic, f"Book it, my number is {PHONE}", tool_llm)
    assert ask(db, clinic, "No", tool_llm).answer == CANCELLED_ANSWER
    tool_llm.script = [[("book_appointment", BOOKING_ARGS)]]
    ask(db, clinic, f"Book it, my number is {PHONE}", tool_llm)
    assert ask(db, clinic, "Cancel", tool_llm, confirm=False).answer == CANCELLED_ANSWER
    assert not booking_api.made("POST")

    tool_llm.script = [[("book_appointment", BOOKING_ARGS)], "Your booking ID is B-1001."]
    ask(db, clinic, f"Book it, my number is {PHONE}", tool_llm)
    assert "B-1001" in ask(db, clinic, "Confirm", tool_llm, confirm=True).answer
    assert len(booking_api.made("POST")) == 1


def test_other_message_replaces_pending_and_malayalam_yes_works(db: Session, clinic, booking_api: BookingAPI) -> None:
    tool_llm = FakeToolLLM([[("book_appointment", BOOKING_ARGS)], "Which doctor would you like instead?"])
    ask(db, clinic, f"Book it, {PHONE}", tool_llm)
    ask(db, clinic, "Actually, a different doctor please", tool_llm)
    tool_llm.script = ["Okay."]
    ask(db, clinic, "yes", tool_llm)
    assert not booking_api.made("POST")  # the pending booking was dropped

    tool_llm.script = [[("book_appointment", BOOKING_ARGS)], "ബുക്ക് ചെയ്തു: B-1001"]
    ask(db, clinic, f"Book it, {PHONE}", tool_llm)
    ask(db, clinic, "അതെ", tool_llm)
    assert len(booking_api.made("POST")) == 1


def test_pending_confirmation_expires(db: Session, clinic, booking_api: BookingAPI) -> None:
    tool_llm = FakeToolLLM([[("book_appointment", BOOKING_ARGS)]])
    ask(db, clinic, f"Book it, {PHONE}", tool_llm)
    state = db.scalar(select(ActionSession))
    assert state.pending_encrypted and PHONE not in state.pending_encrypted  # encrypted at rest
    state.pending_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert ask(db, clinic, "Confirm", tool_llm, confirm=True).answer == EXPIRED_ANSWER
    tool_llm.script = ["How can I help?"]
    ask(db, clinic, "yes", tool_llm)
    assert not booking_api.made("POST")


# ----------------------------------------------------------------------------- validation
def _action(**params: dict[str, Any]) -> ClientAction:
    return ClientAction(name="a", method="POST", path="/x", parameters=[{"name": k, **v} for k, v in params.items()])


@pytest.mark.parametrize(
    ("spec", "value", "message"),
    [
        ({"type": "date"}, "10/10/2026", "ISO date"),
        ({"type": "phone"}, "call me", "phone number"),
        ({"type": "email"}, "not-an-email", "email"),
        ({"type": "integer"}, "3.5", "whole number"),
        ({"type": "string", "enum": ["cardiology", "ent"]}, "dentistry", "one of"),
        ({"type": "string"}, "x" * 501, "too long"),
        ({"type": "boolean"}, "maybe", "true or false"),
    ],
)
def test_parameter_validation_rejects_bad_input(spec: dict[str, Any], value: Any, message: str) -> None:
    with pytest.raises(ActionError, match=message):
        validate_params(_action(v={"location": "body", **spec}), {"v": value})


def test_parameter_validation_required_unknown_and_clean_values() -> None:
    action = _action(phone={"type": "phone", "required": True, "location": "body"}, day={"type": "date", "location": "body"})
    with pytest.raises(ActionError, match="Missing required"):
        validate_params(action, {})
    with pytest.raises(ActionError, match="Unknown parameter"):
        validate_params(action, {"phone": PHONE, "admin": True})
    assert validate_params(action, {"phone": "+91 98765-43210", "day": "2026-10-10"}) == {"phone": "+919876543210", "day": "2026-10-10"}


def test_invalid_booking_arguments_are_not_made_pending(db: Session, clinic, booking_api: BookingAPI) -> None:
    tool_llm = FakeToolLLM([[("book_appointment", {**BOOKING_ARGS, "date": "tomorrow"})], "Which date exactly?"])
    result = ask(db, clinic, f"Book tomorrow, {PHONE}", tool_llm)
    assert result.confirmation is None and result.answer == "Which date exactly?"
    assert "Not run" in tool_llm.calls[1]["messages"][-1]["content"]


# ----------------------------------------------------------------------------- SSRF
@pytest.mark.parametrize("ip", ["10.0.0.5", "172.16.3.4", "192.168.1.1", "127.0.0.1", "169.254.169.254", "::1", "fd00::1", "::ffff:10.0.0.1"])
def test_ssrf_blocks_private_addresses_in_cloud_mode(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    monkeypatch.setattr(executor, "resolve_host", lambda host: {ip})
    with pytest.raises(ActionError, match="private or internal"):
        check_target("https://api.example.com/v1", allow_private=False)
    check_target("https://api.example.com/v1", allow_private=True)  # on-premise may call internal APIs


def test_ssrf_rules_on_save_and_at_call_time(db: Session, clinic, booking_api: BookingAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.actions import ActionConfigError

    with pytest.raises(ActionConfigError, match="https"):
        update_settings(clinic, {"base_url": "http://booking.test"}, allow_private=False)
    monkeypatch.setattr(executor, "resolve_host", lambda host: {"10.1.2.3"})
    with pytest.raises(ActionConfigError, match="private"):
        update_settings(clinic, {"base_url": "https://internal.test"}, allow_private=False)
    # DNS now points the saved host at a private address: the call is refused at call time.
    tool_llm = FakeToolLLM([[("get_slots", {"doctor_id": 3, "date": "2026-10-10"})], "Sorry."])
    result = ask(db, clinic, "Slots for Dr. Anil on 2026-10-10?", tool_llm)
    assert not booking_api.requests and "private or internal" in result.action_results[0]["error"]


def test_redirects_are_not_followed(db: Session, clinic, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "http_client", lambda timeout: httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(302, headers={"Location": "http://169.254.169.254/"})), follow_redirects=False))
    tool_llm = FakeToolLLM([[("get_slots", {"doctor_id": 3, "date": "2026-10-10"})], "Sorry."])
    result = ask(db, clinic, "Slots?", tool_llm)
    assert "redirect" in result.action_results[0]["error"]


# ----------------------------------------------------------------------------- loop limits + fallback
def test_tool_loop_stops_after_four_rounds(db: Session, clinic, booking_api: BookingAPI) -> None:
    tool_llm = FakeToolLLM([[("get_slots", {"doctor_id": 3, "date": "2026-10-10"})]] * 10)
    result = ask(db, clinic, "Slots?", tool_llm)
    assert len(booking_api.made("GET")) == MAX_TOOL_ROUNDS
    assert len(tool_llm.calls) == MAX_TOOL_ROUNDS + 1 and tool_llm.calls[-1]["tools"] == []
    assert result.answer == "I couldn't finish that."


def test_model_without_tool_support_falls_back_to_normal_answer(db: Session, clinic, booking_api: BookingAPI) -> None:
    tool_llm, llm = FakeToolLLM(unsupported=True), FakeLLM(reply="Please call the clinic to book.")
    result = ask(db, clinic, "Book an appointment", tool_llm, llm)
    assert result.answer == "Please call the clinic to book." and len(llm.calls) == 1
    assert "LIVE ACTIONS" not in llm.calls[0][1][0]["content"] and not booking_api.requests


def test_fallback_model_is_used_when_primary_fails(db: Session, clinic, booking_api: BookingAPI) -> None:
    from app.services.ai_models import save_model

    backup = save_model(db, {"name": "Backup", "provider": "openai_compatible", "base_url": "http://llm2.test/v1", "model_name": "backup-model"})
    set_setting(db, "fallback_model_id", backup.id)
    db.commit()
    tool_llm = FakeToolLLM(["Hello from the backup."], fail_models={"main-model"})
    result = ask(db, clinic, "Hi", tool_llm)
    assert result.answer == "Hello from the backup." and result.used_fallback


# ----------------------------------------------------------------------------- rate limits
def test_rate_limits_for_calls_and_confirmed_changes(db: Session, clinic, booking_api: BookingAPI) -> None:
    update_setting_dict(db, "rate_limits", {"action_calls_per_session_per_hour": 2, "confirmed_actions_per_session_per_day": 1})
    db.commit()
    slots = ("get_slots", {"doctor_id": 3, "date": "2026-10-10"})
    tool_llm = FakeToolLLM([[slots, slots, slots], "Here you go."])
    result = ask(db, clinic, "Slots?", tool_llm, session="limits-session-1")
    assert [r["ok"] for r in result.action_results] == [True, True, False]
    assert "Rate limit" in result.action_results[2]["error"] and len(booking_api.made("GET")) == 2

    tool_llm = FakeToolLLM([[("book_appointment", BOOKING_ARGS)], "Booked B-1001.", [("book_appointment", BOOKING_ARGS)]])
    ask(db, clinic, f"Book, {PHONE}", tool_llm, session="limits-session-2")
    ask(db, clinic, "yes", tool_llm, session="limits-session-2")
    ask(db, clinic, f"Book another, {PHONE}", tool_llm, session="limits-session-2")
    assert ask(db, clinic, "yes", tool_llm, session="limits-session-2").answer == LIMIT_ANSWER
    assert len(booking_api.made("POST")) == 1


# ----------------------------------------------------------------------------- audit log
def test_action_calls_are_logged_with_masked_personal_data(db: Session, clinic, booking_api: BookingAPI) -> None:
    tool_llm = FakeToolLLM([[("book_appointment", {**BOOKING_ARGS, "patient_name": "Sobish sobish@example.com"})], "Booked B-1001."])
    ask(db, clinic, f"Book, {PHONE}", tool_llm)
    ask(db, clinic, "yes", tool_llm)
    call = db.scalar(select(ActionCall))
    assert call.ok and call.status_code == 201 and call.action_name == "book_appointment" and call.question_id is not None
    stored = json.dumps(call.request_summary)
    assert PHONE not in stored and "sobish@example.com" not in stored
    assert call.request_summary["params"]["phone"] == "[phone]" and "[email]" in call.request_summary["params"]["patient_name"]
    question_texts = " ".join(q for q, in db.execute(select(Question.question)))
    assert PHONE not in question_texts


# ----------------------------------------------------------------------------- admin API + secrecy
def _admin(db: Session, monkeypatch: pytest.MonkeyPatch):
    from tests.test_api import admin_client, finish_setup

    root = admin_client()
    finish_setup(db, root, monkeypatch)
    return root


def test_key_is_never_returned_and_client_admin_cannot_change_settings(db: Session, clinic, booking_api: BookingAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_api import admin_client

    root = _admin(db, monkeypatch)
    url = "/api/admin/clients/clinic/api-actions"
    view = root.get(url)
    assert view.status_code == 200 and API_KEY not in view.text and "api_key_encrypted" not in view.text
    assert view.json()["settings"]["api_key_masked"] == "••••1234" and view.json()["enabled"]
    for path in ("/api/admin/clients/clinic", "/api/admin/clients", f"{url}/calls"):
        assert API_KEY not in root.get(path).text

    # Saving without a key keeps the stored key.
    assert root.put(f"{url}/settings", json={"api_key": "", "timeout_seconds": 10}).json()["settings"]["has_api_key"]

    client_id = root.get("/api/admin/clients/clinic").json()["client"]["id"]
    root.post("/api/admin/users", json={"name": "Ann", "email": "ann@example.com", "password": "ann-password-1", "role": "client_admin", "client_ids": [client_id]})
    ann = admin_client()
    assert ann.post("/api/admin/auth/login", json={"email": "ann@example.com", "password": "ann-password-1"}).status_code == 200
    seen = ann.get(url)
    assert seen.status_code == 200 and not seen.json()["can_edit"] and API_KEY not in seen.text
    action_id = seen.json()["actions"][0]["id"]
    assert ann.put(f"{url}/settings", json={"enabled": False}).status_code == 403
    assert ann.put(f"{url}/settings", json={"base_url": "https://evil.test"}).status_code == 403
    assert ann.post(f"{url}/actions", json={"name": "x_y", "method": "GET", "path": "/x", "description": "x"}).status_code == 403
    assert ann.delete(f"{url}/actions/{action_id}").status_code == 403
    assert ann.post(f"{url}/actions/{action_id}/test", json={"params": {}}).status_code == 403
    assert ann.post(f"{url}/test-connection").status_code == 403
    assert ann.get(f"{url}/calls").status_code == 200


def test_public_chat_never_exposes_api_details(db: Session, clinic, booking_api: BookingAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    tool_llm = FakeToolLLM([[("book_appointment", BOOKING_ARGS)]])
    defaults = list(chat_service.answer_question.__defaults__)
    defaults[-2] = tool_llm  # tool_llm is the second-to-last default
    monkeypatch.setattr(chat_service.answer_question, "__defaults__", tuple(defaults))
    api = TestClient(create_public_app())
    config = api.get("/api/client/clinic/config", headers={"Origin": ORIGIN}).text
    reply = api.post("/api/chat", headers={"Origin": ORIGIN}, json={"client_id": "clinic", "session_id": "public-session-1", "message": f"Book, {PHONE}"})
    body = reply.json()
    assert body["confirmation"]["action"] == "book_appointment" and set(body["confirmation"]) == {"action", "summary"}
    for text in (config, reply.text):
        assert API_KEY not in text and "booking.test" not in text and "/appointments" not in text and "api_settings" not in text


def test_admin_crud_test_and_live_flag(db: Session, clinic, booking_api: BookingAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _admin(db, monkeypatch)
    url = "/api/admin/clients/clinic/api-actions"
    created = root.post(f"{url}/actions", json={
        "name": "cancel_visit", "method": "DELETE", "path": "/appointments/{id}", "description": "Cancel a visit",
        "requires_confirmation": False, "parameters": [{"name": "id", "type": "string", "location": "path"}],
    })
    assert created.status_code == 200
    action = next(a for a in created.json()["actions"] if a["name"] == "cancel_visit")
    assert action["requires_confirmation"] is True  # forced for non-GET
    bad = root.post(f"{url}/actions", json={"name": "Bad Name", "method": "GET", "path": "/x", "description": "x"})
    assert bad.status_code == 400
    mismatch = root.post(f"{url}/actions", json={"name": "get_x", "method": "GET", "path": "/x/{id}", "description": "x"})
    assert mismatch.status_code == 400 and "path" in mismatch.json()["detail"]

    assert root.post(f"{url}/actions/{action['id']}/test", json={"params": {"id": "B-9"}}).status_code == 400  # needs live flag
    ran = root.post(f"{url}/actions/{action['id']}/test", json={"params": {"id": "B-9"}, "live": True}).json()
    assert ran["ok"] and ran["status_code"] == 200 and "B-9" in ran["response"]
    slots = next(a for a in root.get(url).json()["actions"] if a["name"] == "get_slots")
    assert root.post(f"{url}/actions/{slots['id']}/test", json={"params": {"doctor_id": 3, "date": "2026-10-10"}}).json()["ok"]
    assert root.post(f"{url}/test-connection").json()["status_code"] == 404  # reached the API (no health path set)
    calls = root.get(f"{url}/calls").json()
    assert calls["total"] == 2 and calls["calls"][0]["channel"] == "admin"

    # Disabling keeps the configuration but chat stops using it at once.
    off = root.put(f"{url}/settings", json={"enabled": False}).json()
    assert not off["enabled"] and len(off["actions"]) == 6 and off["settings"]["has_api_key"]
    db.expire_all()  # the change was made through the API's own session
    tool_llm = FakeToolLLM()
    ask(db, clinic, "Slots?", tool_llm)
    assert not tool_llm.calls


# ----------------------------------------------------------------------------- template ↔ demo mock API
def test_appointment_template_works_against_the_demo_mock_api(db: Session, embedder, make_client, ai_model, monkeypatch: pytest.MonkeyPatch) -> None:
    from demo import mock_booking_api

    mock = TestClient(mock_booking_api.app, base_url="http://host.docker.internal:8100")
    monkeypatch.setattr(executor, "http_client", lambda timeout: mock)
    set_setting(db, "mode", "onprem")  # the demo API is on a private address
    client = make_client("hospital", "Demo Hospital")
    update_settings(client, {"enabled": True, "base_url": "http://host.docker.internal:8100", "auth_name": "X-API-Key", "api_key": "demo-key"}, allow_private=True)
    add_template(db, client)
    db.commit()

    tool_llm = FakeToolLLM([
        [("list_departments", {})],
        [("list_doctors", {"department_id": 1})],
        [("get_slots", {"doctor_id": 1, "date": "2026-10-12"})],
        "Dr. Anil Kumar is free at 09:00 and 09:30 on Monday.",
    ])
    first = ask(db, client, "I need a cardiologist on 12 October", tool_llm, session="demo-session-1")
    assert [r["ok"] for r in first.action_results] == [True, True, True]
    assert "Cardiology" in first.action_results[0]["response"] and "09:30" in first.action_results[2]["response"]

    booking = {"doctor_id": 1, "date": "2026-10-12", "time": "09:30", "patient_name": "Sobish", "phone": "[phone_1]"}
    tool_llm.script = [[("book_appointment", booking)], "Booked."]
    assert ask(db, client, "Book 09:30 for Sobish, phone +91 98765 43210", tool_llm, session="demo-session-1").confirmation
    done = ask(db, client, "yes", tool_llm, session="demo-session-1")
    result = done.action_results[0]
    assert result["ok"] and result["status_code"] == 201 and "BK-" in result["response"]

    booking_id = json.loads(result["response"])["booking_id"]
    tool_llm.script = [[("cancel_appointment", {"appointment_id": booking_id})], "Cancelled."]
    ask(db, client, f"Cancel {booking_id}", tool_llm, session="demo-session-1")
    cancelled = ask(db, client, "yes", tool_llm, session="demo-session-1")
    assert cancelled.action_results[0]["ok"] and booking_id in cancelled.action_results[0]["response"]


# ----------------------------------------------------------------------------- complete_with_tools (real LiteLLM objects)
TOOLS = [{"type": "function", "function": {"name": "get_slots", "parameters": {"type": "object", "properties": {}}}}]


def _mock_litellm(monkeypatch: pytest.MonkeyPatch, **mock: Any) -> list[dict[str, Any]]:
    import litellm

    seen: list[dict[str, Any]] = []
    real = litellm.completion

    def fake(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(**kwargs, **mock)

    monkeypatch.setattr(litellm, "completion", fake)
    return seen


def test_complete_with_tools_parses_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm.client import ModelConfig, complete_with_tools

    seen = _mock_litellm(monkeypatch, mock_tool_calls=[
        {"id": "call_1", "type": "function", "function": {"name": "get_slots", "arguments": '{"doctor_id": 3, "date": "2026-10-10"}'}},
        {"id": "call_2", "type": "function", "function": {"name": "get_slots", "arguments": "{not json"}},
    ])
    cfg = ModelConfig(provider="openai", model_name="gpt-4o-mini", api_key="sk-test")
    result = complete_with_tools(cfg, [{"role": "user", "content": "slots?"}], TOOLS)
    assert seen[0]["tools"] == TOOLS and seen[0]["tool_choice"] == "auto"
    first, second = result.tool_calls
    assert (first.id, first.name, first.arguments) == ("call_1", "get_slots", {"doctor_id": 3, "date": "2026-10-10"})
    assert "_invalid_arguments" in second.arguments  # rejected later by parameter validation


def test_complete_with_tools_without_tools_is_plain_and_detects_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    from app.llm.client import ModelConfig, ToolsUnsupported, complete_with_tools

    seen = _mock_litellm(monkeypatch, mock_response="Plain answer.")
    cfg = ModelConfig(provider="openai", model_name="gpt-4o-mini", api_key="sk-test")
    result = complete_with_tools(cfg, [{"role": "user", "content": "hi"}], [])
    assert result.text == "Plain answer." and result.tool_calls == [] and "tools" not in seen[0]

    class UnsupportedParamsError(Exception):
        pass

    def reject(**kwargs: Any) -> Any:
        raise UnsupportedParamsError("openai does not support parameters: ['tools']")

    monkeypatch.setattr(litellm, "completion", reject)
    with pytest.raises(ToolsUnsupported):
        complete_with_tools(cfg, [{"role": "user", "content": "hi"}], TOOLS)
