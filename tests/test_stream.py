"""Streamed answers: visible-text filter, fallback, logging, and the streaming endpoints."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat import stream as stream_module
from app.chat.stream import VisibleText, stream_answer
from app.db.models import Question
from app.llm.client import LLMError, LLMResult, ModelConfig
from app.public_app import create_public_app

ORIGIN = "https://www.mall.test"


def fake_stream(pieces: list[str], fail_models: set[str] | None = None):
    def run(cfg: ModelConfig, messages: list[dict[str, str]]) -> Iterator[str | LLMResult]:
        if cfg.model_name in (fail_models or set()):
            raise LLMError(f"{cfg.model_name} is unreachable")
        yield from pieces
        yield LLMResult(text="".join(pieces), input_tokens=100, output_tokens=20, response_ms=5, cost=0.001,
                        model_id=cfg.model_id, model_name=cfg.display_name or cfg.model_name)
    return run


def visible(pieces: list[str]) -> str:
    v = VisibleText()
    return "".join(v.feed(p) for p in pieces)


def test_visible_text_hides_tags_and_reasoning_even_half_written() -> None:
    assert visible(["We open ", "at 9 AM."]) == "We open at 9 AM."
    assert visible(["Sorry, I don't know. [", "[NO_", "ANSWER]]"]) == "Sorry, I don't know. "
    assert visible(["<th", "ink>let me think</th", "ink>Hello", " there"]) == "Hello there"
    assert visible(["A list [1] item"]) == "A list [1] item"  # ordinary brackets are shown


@pytest.fixture
def mall(db: Session, embedder, make_client, ai_model):
    return make_client("mall", "Test Mall", "https://www.mall.test", ["mall.test"])


def test_stream_answer_yields_words_then_done_and_logs(db: Session, mall) -> None:
    events = list(stream_answer(mall.id, "stream-session-1", "Where is ASICS?", stream_llm=fake_stream(["It is ", "on the ", "second floor."])))
    deltas = [v for k, v in events if k == "delta"]
    kind, response = events[-1]
    assert deltas == ["It is ", "on the ", "second floor."] and kind == "done"
    assert response.answer == "It is on the second floor." and response.input_tokens == 100
    row = db.scalar(select(Question).where(Question.session_id == "stream-session-1"))
    assert row.answer == "It is on the second floor."


def test_stream_falls_back_when_the_main_model_fails_first(db: Session, mall) -> None:
    from app.services.ai_models import save_model
    from app.services.settings import set_setting

    backup = save_model(db, {"name": "Backup", "provider": "openai_compatible", "base_url": "http://llm2.test/v1", "model_name": "backup-model"})
    set_setting(db, "fallback_model_id", backup.id)
    db.commit()
    events = list(stream_answer(mall.id, "stream-session-2", "Hi", stream_llm=fake_stream(["Hello!"], fail_models={"main-model"})))
    assert events[-1][1].answer == "Hello!" and events[-1][1].used_fallback

    failing = list(stream_answer(mall.id, "stream-session-3", "Hi", stream_llm=fake_stream([], fail_models={"main-model", "backup-model"})))
    assert failing[-1][1].error and failing[-1][1].answer.startswith("Sorry")


def test_public_stream_endpoint(db: Session, mall, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stream_module, "stream_complete", fake_stream(["We are open ", "until 11 PM."]))
    api = TestClient(create_public_app())
    resp = api.post("/api/chat/stream", headers={"Origin": ORIGIN}, json={"client_id": "mall", "session_id": "public-stream-1", "message": "When do you close?"})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["access-control-allow-origin"] == ORIGIN
    events = [json.loads(line[5:]) for line in resp.text.split("\n\n") if line.startswith("data:")]
    assert [e["text"] for e in events if e["t"] == "delta"] == ["We are open ", "until 11 PM."]
    assert events[-1]["t"] == "done" and events[-1]["answer"] == "We are open until 11 PM." and events[-1]["sources"] == []
    assert api.post("/api/chat/stream", headers={"Origin": "https://evil.test"}, json={"client_id": "mall", "session_id": "public-stream-2", "message": "x"}).status_code == 403
    assert api.options("/api/chat/stream", headers={"Origin": ORIGIN}).status_code == 204
