"""Streamed answers: the visitor sees the reply as the model writes it.

The first words usually arrive within about a second, instead of after the whole answer
(2-3 s). Clients with API actions get the whole answer as one event, because a confirmation
has to be decided before anything is shown.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from typing import Any

from app.chat.prompt import NO_ANSWER_TAG
from app.chat.service import ChatResponse, answer_question, prepare_answer
from app.db.models import Client
from app.db.session import new_session
from app.embeddings.model import Embedder, EmbeddingNotReady
from app.llm.client import LLMError, LLMResult, ModelConfig, stream_complete

logger = logging.getLogger(__name__)

StreamLLM = Callable[[ModelConfig, list[dict[str, str]]], Iterator["str | LLMResult"]]
_TAG_RE = re.compile(r"\[\[\s*NO[_ ]ANSWER\s*\]\]", re.IGNORECASE)


_TAG_LETTERS = re.sub(r"[\s_]", "", NO_ANSWER_TAG.upper())  # "[[NOANSWER]]"


def _hold_back_tag(text: str) -> str:
    """Cut a trailing piece that could be the start of [[NO_ANSWER]] (it may still be completed)."""
    for start in range(max(0, len(text) - len(NO_ANSWER_TAG) - 4), len(text)):
        if text[start] == "[" and _TAG_LETTERS.startswith(re.sub(r"[\s_]", "", text[start:].upper())):
            return text[:start]
    return text


class VisibleText:
    """Turns the model's raw output into text that is safe to show while it is still being written:
    hidden reasoning (<think>…</think>) and the [[NO_ANSWER]] tag never reach the visitor, even half-written."""

    def __init__(self) -> None:
        self.raw = ""
        self.sent = 0

    def _visible(self) -> str:
        text = re.sub(r"<think>.*?</think>", "", self.raw, flags=re.DOTALL)
        if "<think>" in text:  # hidden reasoning still being written
            text = text[: text.index("<think>")]
        elif text.strip() and "<think>".startswith(text.strip()):
            return ""  # might be the start of "<think>"
        text = _TAG_RE.sub("", text)
        return _hold_back_tag(text).lstrip()

    def feed(self, piece: str) -> str:
        """Add model output; return the new text to show (may be empty)."""
        self.raw += piece
        visible = self._visible()
        if len(visible) <= self.sent:
            return ""
        new, self.sent = visible[self.sent :], len(visible)
        return new


def _model_stream(stream_llm: StreamLLM, chain: list[ModelConfig], messages: list[dict[str, str]]) -> Iterator[tuple[str | LLMResult, bool]]:
    """Stream from the models in priority order; one that fails before writing anything hands over to the next."""
    for i, cfg in enumerate(chain):
        wrote = False
        try:
            for item in stream_llm(cfg, messages):
                wrote = wrote or isinstance(item, str)
                yield item, i > 0
            return
        except LLMError as exc:
            if wrote or i + 1 == len(chain):
                raise
            logger.warning("Model %s failed (%s); trying %s", cfg.display_name, exc, chain[i + 1].display_name)


def stream_answer(
    client_pk: int,
    session_id: str,
    message: str,
    channel: str = "widget",
    confirm: bool | None = None,
    embedder: Embedder | None = None,
    stream_llm: StreamLLM | None = None,
) -> Iterator[tuple[str, Any]]:
    """Yield ("delta", text) while the answer is written, then ("done", ChatResponse).

    Uses its own database session: the request's session is closed before a streamed response starts.
    """
    db = new_session()
    try:
        client = db.get(Client, client_pk)
        assert client is not None
        if client.api_actions_enabled:
            from app.actions.flow import enabled_actions

            if enabled_actions(db, client):
                yield "done", answer_question(db, client, session_id, message, channel, embedder, confirm)
                return

        prepared = prepare_answer(db, client, session_id, message, channel)
        visible = VisibleText()
        try:
            chain, messages = prepared.model_inputs(db, embedder)
            db.commit()  # don't hold a transaction open while the model writes
            final: LLMResult | None = None
            used_fallback = False
            for item, from_fallback in _model_stream(stream_llm or stream_complete, chain, messages):
                used_fallback = from_fallback
                if isinstance(item, LLMResult):
                    final = item
                elif piece := visible.feed(item):
                    yield "delta", piece
            if final is None:
                raise LLMError("The model returned no answer.")
            prepared.apply(final, used_fallback)
        except (LLMError, EmbeddingNotReady) as exc:
            prepared.fail(exc)
        response: ChatResponse = prepared.finish(db)
        yield "done", response
    finally:
        db.close()
