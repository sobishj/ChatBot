"""Indexer, hybrid search and chat pipeline (Postgres + fake embedder + fake LLM)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat.prompt import NO_ANSWER_TAG
from app.chat.service import answer_question
from app.db.models import Chunk, Page, Question
from app.indexer.indexer import Source, index_source
from app.search.hybrid import search
from app.services.ai_models import save_model
from app.services.settings import set_setting
from tests.fakes import FakeLLM

pytestmark = pytest.mark.integration


def add_page(db: Session, embedder, client, url: str, title: str, text: str) -> Page:
    page = Page(client_id=client.id, url=url, title=title, content=text, content_hash="h")
    db.add(page)
    db.commit()
    index_source(db, embedder, Source(client.id, "web", url, title, text, page_id=page.id))
    return page


@pytest.fixture
def mall(db: Session, embedder, make_client):
    client = make_client("mall", "Test Mall", "https://www.mall.test")
    add_page(db, embedder, client, "https://www.mall.test/shop/asics/", "ASICS", "ASICS sports shoes.\nLocated on\nSecond Floor\nShop Timings 10:00 AM to 10:00 PM")
    add_page(db, embedder, client, "https://www.mall.test/shop/basics-life/", "Basics Life", "Basics Life fashion.\nLocated on\nFirst Floor")
    add_page(db, embedder, client, "https://www.mall.test/movies/", "Movies", "Now showing: Zootopia 2, Dheeram, Pongala")
    return client


def test_index_replaces_old_chunks(db: Session, embedder, mall) -> None:
    page = db.scalar(select(Page).where(Page.url.like("%asics%")))
    before = db.scalar(select(Chunk.id).where(Chunk.page_id == page.id))
    index_source(db, embedder, Source(mall.id, "web", page.url, "ASICS", "ASICS moved to Third Floor", page_id=page.id))
    rows = list(db.scalars(select(Chunk).where(Chunk.page_id == page.id)))
    assert len(rows) == 1 and "Third Floor" in rows[0].content and rows[0].id != before


def test_hybrid_search_keyword_beats_lookalike(db: Session, embedder, mall) -> None:
    result = search(db, embedder, mall.id, "Where is the ASICS store?")
    assert result.hits[0].source.endswith("/shop/asics/")
    assert result.keyword_match
    assert 0 < result.confidence <= 1
    # 'asics' must never match 'basics' in full-text search
    assert all("basics" not in h.source or h.text_rank is None for h in result.hits)


def test_search_is_isolated_per_client(db: Session, embedder, mall, make_client) -> None:
    other = make_client("other", "Other Co", "https://other.test")
    add_page(db, embedder, other, "https://other.test/secret/", "Secret", "ASICS secret partner discount code XYZ")
    hits = search(db, embedder, mall.id, "ASICS secret discount code").hits
    assert hits and all(h.source.startswith("https://www.mall.test") for h in hits)
    other_hits = search(db, embedder, other.id, "ASICS").hits
    assert all(h.source.startswith("https://other.test") for h in other_hits)


def test_chat_answers_with_sources_and_logs(db: Session, embedder, mall, ai_model) -> None:
    llm = FakeLLM(reply="ASICS is on the Second Floor, open 10:00 AM to 10:00 PM.")
    set_setting(db, "confidence_threshold", 0.1)
    db.commit()
    r = answer_question(db, mall, "session-123456", "Where is the ASICS store?", llm=llm)
    assert r.answered and r.sources and r.sources[0]["url"].endswith("/shop/asics/")
    system_prompt = llm.calls[0][1][0]["content"]
    assert "Second Floor" in system_prompt and "Test Mall" in system_prompt
    row = db.get(Question, r.question_id)
    assert row.answered and row.model_name == "Main" and row.input_tokens == 100 and row.channel == "widget"


def test_chat_unanswered_by_tag_and_threshold(db: Session, embedder, mall, ai_model) -> None:
    llm = FakeLLM(reply=f"Sorry, I don't have that information. {NO_ANSWER_TAG}")
    r = answer_question(db, mall, "session-123456", "Do you sell helicopters?", llm=llm)
    assert not r.answered and NO_ANSWER_TAG not in r.answer and r.sources == []
    assert db.get(Question, r.question_id).answered is False

    set_setting(db, "confidence_threshold", 0.99)
    db.commit()
    r2 = answer_question(db, mall, "session-123456", "Where is ASICS?", llm=FakeLLM(reply="Second floor"))
    assert not r2.answered


def test_chat_masks_pii_before_model_and_db(db: Session, embedder, mall, ai_model) -> None:
    llm = FakeLLM()
    r = answer_question(db, mall, "session-123456", "Call me at +91 98470 12345 or mail a.b@example.com", llm=llm)
    row = db.get(Question, r.question_id)
    assert "98470" not in row.question and "example.com" not in row.question
    assert "[phone]" in row.question and "[email]" in row.question
    assert "98470" not in llm.calls[0][1][-1]["content"]


def test_chat_history_last_four_messages(db: Session, embedder, mall, ai_model) -> None:
    llm = FakeLLM()
    for q in ("first question", "second question", "third question"):
        answer_question(db, mall, "session-abcdef", q, llm=llm)
    messages = llm.calls[-1][1]
    # system + 2 previous Q/A pairs (4 messages) + current question
    assert len(messages) == 6
    assert messages[1]["content"] == "first question" and messages[3]["content"] == "second question"


def test_chat_uses_client_model_and_fallback(db: Session, embedder, mall, ai_model) -> None:
    special = save_model(db, {"name": "Special", "provider": "openai_compatible", "base_url": "http://x/v1", "model_name": "special-model"})
    backup = save_model(db, {"name": "Backup", "provider": "openai_compatible", "base_url": "http://y/v1", "model_name": "backup-model"})
    mall.ai_model_id = special.id
    set_setting(db, "fallback_model_id", backup.id)
    db.commit()
    db.refresh(mall)

    llm = FakeLLM()
    r = answer_question(db, mall, "session-123456", "Where is ASICS?", llm=llm)
    assert llm.calls[-1][0] == "special-model" and not r.used_fallback

    llm = FakeLLM(fail_models={"special-model"})
    r = answer_question(db, mall, "session-123456", "Where is ASICS?", llm=llm)
    assert [c[0] for c in llm.calls] == ["special-model", "backup-model"]
    assert r.used_fallback and r.model_name == "Backup"
    assert db.get(Question, r.question_id).used_fallback


def test_chat_all_models_down_is_logged(db: Session, embedder, mall, ai_model) -> None:
    r = answer_question(db, mall, "session-123456", "Where is ASICS?", llm=FakeLLM(fail_models={"main-model"}))
    assert r.error and not r.answered
    row = db.get(Question, r.question_id)
    assert row.error and row.answer == r.answer


def test_chat_without_model_configured(db: Session, embedder, mall) -> None:
    r = answer_question(db, mall, "session-123456", "Hello", llm=FakeLLM())
    assert r.error and "No AI model" in r.error
