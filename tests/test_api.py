"""HTTP tests for the public API and the admin API (Postgres, fake embedder and LLM)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.admin_app import create_admin_app
from app.chat import service as chat_service
from app.db.models import Question
from app.public_app import create_public_app
from app.services.settings import set_setting, update_setting_dict
from tests.fakes import FakeLLM

pytestmark = pytest.mark.integration

ORIGIN = "https://www.mall.test"


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    llm = FakeLLM(reply="It is on the Second Floor.")
    monkeypatch.setattr(chat_service, "complete", llm)
    # answer_question binds `complete` as a default argument; patch the default too.
    defaults = list(chat_service.answer_question.__defaults__)
    defaults[-1] = llm
    monkeypatch.setattr(chat_service.answer_question, "__defaults__", tuple(defaults))
    return llm


@pytest.fixture
def mall(db: Session, embedder, make_client, ai_model):
    client = make_client("mall", "Test Mall", "https://www.mall.test", ["mall.test"])
    return client


# ----------------------------------------------------------------------------- public API
def test_public_config_requires_allowed_origin(db: Session, mall) -> None:
    api = TestClient(create_public_app())
    ok = api.get("/api/client/mall/config", headers={"Origin": ORIGIN})
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == ORIGIN
    body = ok.json()
    assert body["bot_name"] and "allowed_domains" not in body and "ai_model_id" not in body
    assert api.get("/api/client/mall/config", headers={"Origin": "https://evil.test"}).status_code == 403
    assert api.get("/api/client/mall/config").status_code == 403  # no origin (e.g. curl)
    # Referer is accepted when a browser omits Origin
    assert api.get("/api/client/mall/config", headers={"Referer": ORIGIN + "/page"}).status_code == 200
    assert api.get("/api/client/nope/config", headers={"Origin": ORIGIN}).status_code == 404


def test_public_chat_flow_and_rejections(db: Session, mall, fake_llm: FakeLLM) -> None:
    api = TestClient(create_public_app())
    body = {"client_id": "mall", "session_id": "sess-12345678", "message": "Where is ASICS? my mail x@y.com"}

    pre = api.options("/api/chat", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == ORIGIN
    assert api.options("/api/chat", headers={"Origin": "https://evil.test"}).status_code == 403

    r = api.post("/api/chat", json=body, headers={"Origin": ORIGIN})
    assert r.status_code == 200 and r.json()["answer"] == "It is on the Second Floor."
    assert r.headers["access-control-allow-origin"] == ORIGIN
    saved = db.query(Question).one()
    assert "[email]" in saved.question and "x@y.com" not in saved.question

    assert api.post("/api/chat", json=body, headers={"Origin": "https://evil.test"}).status_code == 403
    assert api.post("/api/chat", json=body).status_code == 403
    assert api.post("/api/chat", json={**body, "session_id": "bad id!"}, headers={"Origin": ORIGIN}).status_code == 422

    mall.active = False
    db.commit()
    assert api.post("/api/chat", json=body, headers={"Origin": ORIGIN}).status_code == 404


def test_public_chat_rate_limit(db: Session, mall, fake_llm: FakeLLM) -> None:
    update_setting_dict(db, "rate_limits", {"chat_per_ip_per_minute": 2})
    db.commit()
    api = TestClient(create_public_app())
    body = {"client_id": "mall", "session_id": "sess-12345678", "message": "hi"}
    codes = [api.post("/api/chat", json=body, headers={"Origin": ORIGIN}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_widget_js_served(db: Session) -> None:
    r = TestClient(create_public_app()).get("/widget.js")
    assert r.status_code == 200 and "attachShadow" in r.text and r.headers["content-type"].startswith("application/javascript")


# ----------------------------------------------------------------------------- admin API helpers
def admin_client() -> TestClient:
    client = TestClient(create_admin_app())
    client.get("/health")  # receive the CSRF cookie
    client.headers["X-CSRF-Token"] = client.cookies.get("wa_csrf") or ""
    return client


def finish_setup(db: Session, api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ai_models

    monkeypatch.setattr(ai_models, "test_connection", lambda cfg: FakeLLM(reply="Hello")(cfg, []))
    assert api.post("/api/admin/setup/admin", json={"name": "Root", "email": "root@example.com", "password": "a-strong-password"}).status_code == 200
    assert api.post("/api/admin/setup/mode", json={"mode": "cloud"}).status_code == 200
    assert api.post("/api/admin/setup/domain", json={"public_domain": "https://chat.example.com/"}).json()["public_domain"] == "chat.example.com"
    model = {"name": "Local Qwen", "provider": "bionic", "base_url": "http://host.docker.internal:7800/v1", "model_name": "qwen"}
    assert api.post("/api/admin/setup/model/test", json=model).json()["ok"] is True
    assert api.post("/api/admin/setup/model", json=model).status_code == 200
    # Embedding download runs in the worker; mark it ready directly.
    update_setting_dict(db, "embedding", {"status": "ready", "model": "fake-embedder", "dim": 64, "path": "/fake"})
    db.commit()
    assert api.post("/api/admin/setup/embedding/continue").json()["step"] == 7
    assert api.post("/api/admin/setup/finish").status_code == 200


# ----------------------------------------------------------------------------- admin API
def test_setup_wizard_and_lockdown(db: Session, embedder, monkeypatch: pytest.MonkeyPatch) -> None:
    api = admin_client()
    assert api.get("/api/admin/dashboard").status_code in (401, 409)
    state = api.get("/api/admin/setup/state").json()
    assert state["step"] == 1 and not state["has_admin"]

    finish_setup(db, api, monkeypatch)

    # Wizard closed for good; a second admin can't be created through it.
    assert api.post("/api/admin/setup/admin", json={"name": "X", "email": "x@example.com", "password": "another-password"}).status_code == 409
    me = api.get("/api/admin/auth/me").json()
    assert me["setup_completed"] and me["user"]["role"] == "super_admin"
    models = api.get("/api/admin/models").json()
    assert models["models"][0]["is_default"] and "api_key_encrypted" not in str(models)


def test_setup_steps_need_login(db: Session) -> None:
    api = admin_client()
    api.post("/api/admin/setup/admin", json={"name": "Root", "email": "root@example.com", "password": "a-strong-password"})
    anon = admin_client()
    assert anon.post("/api/admin/setup/mode", json={"mode": "onprem"}).status_code == 401


def test_csrf_and_login_rate_limit(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    api = admin_client()
    finish_setup(db, api, monkeypatch)
    no_csrf = TestClient(create_admin_app())
    assert no_csrf.post("/api/admin/auth/login", json={"email": "root@example.com", "password": "a-strong-password"}).status_code == 403

    update_setting_dict(db, "rate_limits", {"login_per_ip_per_15min": 3})
    db.commit()
    fresh = admin_client()
    codes = [fresh.post("/api/admin/auth/login", json={"email": "root@example.com", "password": "wrong-password"}).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]


def test_client_admin_restrictions(db: Session, embedder, monkeypatch: pytest.MonkeyPatch) -> None:
    root = admin_client()
    finish_setup(db, root, monkeypatch)
    a = root.post("/api/admin/clients", json={"client_id": "client-a", "name": "Client A", "website_url": "https://a.test"}).json()["client"]
    root.post("/api/admin/clients", json={"client_id": "client-b", "name": "Client B", "website_url": "https://b.test"})
    assert root.post("/api/admin/users", json={"name": "Ann", "email": "ann@example.com", "password": "ann-password-1", "role": "client_admin", "client_ids": [a["id"]]}).status_code == 200

    ann = admin_client()
    assert ann.post("/api/admin/auth/login", json={"email": "ann@example.com", "password": "ann-password-1"}).status_code == 200
    listed = [c["client_id"] for c in ann.get("/api/admin/clients").json()["clients"]]
    assert listed == ["client-a"]
    assert ann.get("/api/admin/clients/client-b").status_code == 404
    assert ann.get("/api/admin/clients/client-b/conversations").status_code == 404
    assert ann.get("/api/admin/models").status_code == 403
    assert ann.get("/api/admin/settings").status_code == 403
    assert ann.get("/api/admin/users").status_code == 403
    assert ann.get("/api/admin/system").status_code == 403
    assert ann.put("/api/admin/clients/client-a/model", json={"ai_model_id": None}).status_code == 403
    assert ann.put("/api/admin/clients/client-a/domains", json={"allowed_domains": ["evil.test"]}).status_code == 403
    # Allowed: branding and stats for their own client
    assert ann.put("/api/admin/clients/client-a/branding", json={"bot_name": "Ana"}).status_code == 200
    assert ann.get("/api/admin/clients/client-a/stats").status_code == 200


def test_client_crud_test_chat_and_stats(db: Session, embedder, monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM) -> None:
    api = admin_client()
    finish_setup(db, api, monkeypatch)
    r = api.post("/api/admin/clients", json={"client_id": "lulu-test", "name": "Mall", "website_url": "www.mall.test"})
    assert r.status_code == 200
    client = r.json()["client"]
    assert client["allowed_domains"] == ["www.mall.test"] and client["website_url"] == "https://www.mall.test"
    assert api.post("/api/admin/clients", json={"client_id": "lulu-test", "name": "Dup"}).status_code == 400

    chat = api.post("/api/admin/clients/lulu-test/test-chat", json={"session_id": "t1", "message": "Call 9847012345"}).json()
    assert chat["answer"] == "It is on the Second Floor." and chat["model_name"] == "Local Qwen"
    conv = api.get("/api/admin/clients/lulu-test/conversations?channel=test").json()
    assert conv["total"] == 1 and "[phone]" in conv["items"][0]["question"]
    stats = api.get("/api/admin/clients/lulu-test/stats?days=7").json()
    assert stats["total"] == 0  # test chats excluded from statistics
    csv = api.get("/api/admin/clients/lulu-test/conversations.csv?channel=test")
    assert csv.status_code == 200 and "[phone]" in csv.text

    embed = api.get("/api/admin/clients/lulu-test/embed").json()
    assert embed["snippet"] == '<script src="https://chat.example.com/widget.js" data-client="lulu-test" defer></script>'

    set_setting(db, "mode", "onprem")
    db.commit()
    assert api.post("/api/admin/clients", json={"client_id": "second", "name": "Second"}).status_code == 400


def test_spa_and_security_headers(db: Session) -> None:
    api = TestClient(create_admin_app())
    r = api.get("/clients/x/stats")
    assert r.status_code in (200, 503)  # index.html (or "not built" page in dev)
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]
    assert api.get("/api/admin/unknown").status_code == 404


def test_switch_between_single_and_multi_client_mode(db: Session, embedder, monkeypatch: pytest.MonkeyPatch) -> None:
    root = admin_client()
    finish_setup(db, root, monkeypatch)
    switch = lambda mode: root.post("/api/admin/settings/mode", json={"mode": mode})  # noqa: E731

    # Single-client mode with no client yet: the first client becomes the assistant.
    assert switch("onprem").status_code == 200
    root.post("/api/admin/clients", json={"client_id": "client-a", "name": "Client A", "website_url": "https://a.test"})
    assert root.get("/api/admin/auth/me").json()["onprem_client_id"] == "client-a"
    blocked = root.post("/api/admin/clients", json={"client_id": "client-b", "name": "Client B"})
    assert blocked.status_code == 400
    assert root.delete("/api/admin/clients/client-a").status_code == 400  # the assistant can't be deleted

    # Multi-client mode: adding clients works again, nothing was lost.
    assert switch("cloud").json()["mode"] == "cloud"
    assert root.get("/api/admin/auth/me").json()["onprem_client_id"] is None
    assert root.post("/api/admin/clients", json={"client_id": "client-b", "name": "Client B"}).status_code == 200

    # Back to single-client is refused while two clients exist, and allowed with one.
    refused = switch("onprem")
    assert refused.status_code == 400 and "Client A" in refused.json()["detail"]
    assert root.get("/api/admin/settings").json()["mode"] == "cloud"
    assert root.delete("/api/admin/clients/client-b").status_code == 200
    assert switch("onprem").json()["mode"] == "onprem"
    assert root.get("/api/admin/auth/me").json()["onprem_client_id"] == "client-a"
    assert switch("sideways").status_code == 400


def test_document_enabled_toggle(db: Session, embedder, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from app.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    api = admin_client()
    finish_setup(db, api, monkeypatch)
    api.post("/api/admin/clients", json={"client_id": "docs", "name": "Docs", "website_url": "https://docs.test"})
    up = api.post("/api/admin/clients/docs/documents", files={"files": ("a.txt", b"Parking costs 40 rupees.", "text/plain")}).json()
    doc = up["saved"][0]
    assert doc["enabled"] is True
    off = api.put(f"/api/admin/clients/docs/documents/{doc['id']}/enabled", json={"enabled": False})
    assert off.status_code == 200 and off.json()["document"]["enabled"] is False
    listed = api.get("/api/admin/clients/docs/documents").json()["documents"]
    assert listed[0]["enabled"] is False
    assert api.put("/api/admin/clients/docs/documents/999999/enabled", json={"enabled": True}).status_code == 404
