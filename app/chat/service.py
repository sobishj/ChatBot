"""Answer a visitor question: search → prompt → model (with fallback) → log.

Shared by the public chat API, the admin "Test chat" tab and the CLI.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat.contact import with_contact
from app.chat.essentials import key_information, page_directory
from app.chat.language import detect_language
from app.chat.pii import mask_pii
from app.chat.prompt import build_messages, parse_answer
from app.db.models import Client, Question
from app.embeddings.model import Embedder, EmbeddingNotReady, get_embedder
from app.llm import health
from app.llm.client import LLMError, LLMResult, ModelConfig, ToolResult, complete, complete_with_tools
from app.search.hybrid import SearchHit, is_list_question, search, words
from app.services.ai_models import resolve_chain
from app.services.clients import branding, document_settings
from app.services.settings import get_settings_map

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1000
HISTORY_PAIRS = 2  # last 4 messages = 2 questions + 2 answers
HISTORY_WINDOW = timedelta(hours=2)
MAX_SOURCES = 3

UNAVAILABLE_ANSWER = "Sorry, I can't answer right now. Please try again in a moment."


@dataclass
class ChatResponse:
    answer: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    answered: bool = False
    confidence: float = 0.0
    model_name: str | None = None
    used_fallback: bool = False
    response_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    language: str | None = None
    question_id: int | None = None
    error: str | None = None
    hits: list[SearchHit] = field(default_factory=list)
    # API actions only (see app.actions): a change waiting for the visitor's confirmation,
    # what ran (Test chat diagnostics) and the action names (logged with the question).
    confirmation: dict[str, str] | None = None
    action_results: list[dict[str, Any]] = field(default_factory=list)
    actions_used: list[str] = field(default_factory=list)


def local_now(client_timezone: str | None, global_timezone: str | None = None) -> datetime:
    """Current time for questions like "is it open now?".

    The client's own time zone (Branding), else the global one (Settings), else UTC.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    for zone in (client_timezone, global_timezone):
        if zone:
            try:
                return datetime.now(ZoneInfo(zone))
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return datetime.now(UTC)


def normalize_question(text: str) -> str:
    """Lower-case, strip punctuation and extra spaces: groups 'Where is ASICS?' with 'where is asics'."""
    return " ".join(words(text))[:500]


def load_history(db: Session, client_id: int, session_id: str, channel: str) -> list[tuple[str, str]]:
    since = datetime.now(UTC) - HISTORY_WINDOW
    rows = db.execute(
        select(Question.question, Question.answer)
        .where(
            Question.client_id == client_id,
            Question.session_id == session_id,
            Question.channel == channel,
            Question.created_at >= since,
            Question.error.is_(None),
        )
        .order_by(Question.created_at.desc(), Question.id.desc())
        .limit(HISTORY_PAIRS)
    ).all()
    return [(q, a) for q, a in reversed(rows)]


def sources_from_hits(hits: list[SearchHit], limit: int = MAX_SOURCES) -> list[dict[str, Any]]:
    """Distinct sources of the best hits: web pages as links, documents by name."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.source in seen:
            continue
        seen.add(hit.source)
        if hit.source_type == "web":
            out.append({"type": "web", "title": hit.title or hit.source, "url": hit.source})
        else:
            out.append({"type": "doc", "title": hit.source})
        if len(out) >= limit:
            break
    return out


def _call_with_fallback(chain: list[ModelConfig], messages: list[dict[str, str]], llm: Callable[..., LLMResult]) -> tuple[LLMResult, bool]:
    """Try the models in priority order (failing ones last); returns (result, whether another model answered)."""
    original = chain
    chain = health.healthy_first(chain)
    for i, cfg in enumerate(chain):
        try:
            result = llm(cfg, messages)
            health.record_success(cfg)
            return result, cfg is not original[0]
        except LLMError as exc:
            health.record_failure(cfg, exc)
            if i + 1 == len(chain):
                raise
            logger.warning("Model %s failed (%s); trying %s", cfg.display_name, exc, chain[i + 1].display_name)
    raise LLMError("No AI model is configured. Add one in Settings → AI Models.")


def answer_question(
    db: Session,
    client: Client,
    session_id: str,
    message: str,
    channel: str = "widget",
    embedder: Embedder | None = None,
    confirm: bool | None = None,
    tool_llm: Callable[..., ToolResult] = complete_with_tools,
    llm: Callable[..., LLMResult] = complete,  # keep last: tests replace the final default
) -> ChatResponse:
    """``confirm`` is the widget's Confirm (True) / Cancel (False) button for a pending API action."""
    if client.api_actions_enabled:
        from app.actions.flow import answer_with_actions, enabled_actions

        actions = enabled_actions(db, client)
        if actions:
            return answer_with_actions(db, client, session_id, message, channel, actions, embedder, llm, tool_llm, confirm)

    prepared = prepare_answer(db, client, session_id, message, channel)
    try:
        chain, messages = prepared.model_inputs(db, embedder)
        llm_result, used_fallback = _call_with_fallback(chain, messages, llm)
        prepared.apply(llm_result, used_fallback)
    except (LLMError, EmbeddingNotReady) as exc:
        prepared.fail(exc)
    return prepared.finish(db)


@dataclass
class PreparedAnswer:
    """One plain (non-actions) answer in progress: shared by normal and streamed answers."""

    client: Client
    session_id: str
    channel: str
    question: str
    language: str | None
    settings: dict[str, Any]
    response: ChatResponse
    started: float
    model_id: int | None = None

    def model_inputs(self, db: Session, embedder: Embedder | None = None) -> tuple[list[ModelConfig], list[dict[str, str]]]:
        """Search, build the prompt and pick the models (in priority order). Raises LLMError / EmbeddingNotReady."""
        b = branding(self.client)
        history = load_history(db, self.client.id, self.session_id, self.channel)
        result = search(db, embedder or get_embedder(), self.client.id, self.question, previous_question=history[-1][0] if history else None)
        self.response.hits = result.hits
        self.response.confidence = round(result.confidence, 4)
        doc_settings = document_settings(self.client)
        messages = build_messages(
            self.settings["system_prompt"], b["bot_name"], self.client.name, result.hits, history, self.question,
            key_information(db, self.client.id, doc_settings.get("key_facts") or ""), local_now(b["timezone"], self.settings["timezone"]),
            page_directory(db, self.client.id) if is_list_question(self.question) else "",
            b.get("contact_details") or "",
        )
        chain = resolve_chain(db, self.client)
        if not chain:
            raise LLMError("No AI model is configured. Add one in Settings → AI Models.")
        if is_list_question(self.question):  # completeness needs the model's reasoning; other answers start faster without it
            chain = [replace(cfg, deep=True) for cfg in chain]
        return chain, messages

    def apply(self, llm_result: LLMResult, used_fallback: bool) -> None:
        response = self.response
        answer, flagged_no_answer = parse_answer(llm_result.text)
        response.answer = answer or UNAVAILABLE_ANSWER
        if not answer or flagged_no_answer:  # the model couldn't answer: make sure the visitor gets the contact details
            response.answer = with_contact(response.answer, branding(self.client).get("contact_details"), self.language)
        response.answered = (
            bool(answer) and not flagged_no_answer and bool(response.hits)
            and response.confidence >= float(self.settings["confidence_threshold"])
        )
        response.sources = sources_from_hits(response.hits) if response.answered else []
        response.model_name = llm_result.model_name
        response.used_fallback = used_fallback
        response.input_tokens = llm_result.input_tokens
        response.output_tokens = llm_result.output_tokens
        response.cost = llm_result.cost
        self.model_id = llm_result.model_id

    def fail(self, exc: Exception) -> None:
        logger.warning("Chat failed for client %s: %s", self.client.client_id, exc)
        self.response.answer = with_contact(UNAVAILABLE_ANSWER, branding(self.client).get("contact_details"), self.language)
        self.response.error = str(exc)[:1000]
        self.model_id = None

    def finish(self, db: Session) -> ChatResponse:
        self.response.response_ms = int((time.perf_counter() - self.started) * 1000)
        save_question(db, self.client, self.session_id, self.channel, self.question, self.language, self.response, self.model_id)
        return self.response


def prepare_answer(db: Session, client: Client, session_id: str, message: str, channel: str) -> PreparedAnswer:
    question = mask_pii(message.strip())[:MAX_MESSAGE_CHARS]
    b = branding(client)
    settings = get_settings_map(db, ["system_prompt", "confidence_threshold", "timezone"])
    language = detect_language(question, b.get("default_language") or "en")
    return PreparedAnswer(
        client=client, session_id=session_id, channel=channel, question=question, language=language, settings=settings,
        response=ChatResponse(answer=UNAVAILABLE_ANSWER, language=language), started=time.perf_counter(),
    )


def save_question(
    db: Session, client: Client, session_id: str, channel: str, question: str, language: str | None, response: ChatResponse, model_id: int | None
) -> Question:
    """Log the (already masked) question and its answer; sets ``response.question_id``. Commits."""
    row = Question(
        client_id=client.id,
        session_id=session_id[:64],
        channel=channel,
        question=question,
        question_normalized=normalize_question(question),
        answer=response.answer,
        sources=response.sources,
        answered=response.answered,
        top_score=response.confidence,
        language=language,
        model_id=model_id,
        model_name=response.model_name,
        used_fallback=response.used_fallback,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cost=response.cost,
        response_ms=response.response_ms,
        error=response.error,
        actions_used=response.actions_used,
    )
    db.add(row)
    db.commit()
    response.question_id = row.id
    return row
