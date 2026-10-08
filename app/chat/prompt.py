"""Build the LLM messages: system prompt (with retrieved context), recent history, question."""

from __future__ import annotations

import re
from datetime import date

from app.search.hybrid import SearchHit

NO_ANSWER_TAG = "[[NO_ANSWER]]"
_NO_ANSWER_RE = re.compile(r"\[\[\s*NO[_ ]ANSWER\s*\]\]", re.IGNORECASE)
MAX_CONTEXT_CHARS = 12_000


def format_context(hits: list[SearchHit]) -> str:
    """Numbered context blocks with their source, trimmed to a safe total size."""
    if not hits:
        return "(no relevant information found)"
    blocks: list[str] = []
    used = 0
    for i, hit in enumerate(hits, start=1):
        label = hit.source if hit.source_type == "web" else f"document: {hit.source}"
        block = f"[{i}] {hit.title or 'Untitled'} ({label})\n{hit.content.strip()}"
        if used + len(block) > MAX_CONTEXT_CHARS and blocks:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


def render_system_prompt(template: str, bot_name: str, client_name: str, context: str, today: date | None = None) -> str:
    """Fill the known placeholders. Plain replacement so other braces in a custom prompt are harmless."""
    values = {
        "{bot_name}": bot_name,
        "{client_name}": client_name,
        "{today}": (today or date.today()).strftime("%A, %d %B %Y"),
    }
    prompt = template
    for key, value in values.items():
        prompt = prompt.replace(key, value)
    if "{context}" in prompt:
        return prompt.replace("{context}", context)
    return f"{prompt}\n\nCONTEXT:\n{context}"


def build_messages(
    template: str,
    bot_name: str,
    client_name: str,
    hits: list[SearchHit],
    history: list[tuple[str, str]],
    question: str,
) -> list[dict[str, str]]:
    """``history`` is a list of (question, answer) pairs, oldest first."""
    system = render_system_prompt(template, bot_name, client_name, format_context(hits))
    messages = [{"role": "system", "content": system}]
    for past_question, past_answer in history:
        messages.append({"role": "user", "content": past_question})
        messages.append({"role": "assistant", "content": past_answer})
    messages.append({"role": "user", "content": question})
    return messages


# Secondary signal for models that forget the tag: common "I don't know" phrasings.
_NO_INFO_RE = re.compile(
    r"\b(?:do(?:es)? not|don't|doesn't|cannot|can't|couldn't|unable to)\s+(?:have|find|provide|see|locate)\b"
    r"[^.?!]{0,40}\b(?:information|details|info|answer)\b"
    r"|\bno (?:information|details) (?:about|on|regarding)\b"
    r"|\bnot (?:mentioned|available|provided) in the (?:context|information)\b",
    re.IGNORECASE,
)


def parse_answer(raw: str) -> tuple[str, bool]:
    """Return (clean answer, model_said_no_answer)."""
    flagged = bool(_NO_ANSWER_RE.search(raw)) or bool(_NO_INFO_RE.search(raw))
    clean = _NO_ANSWER_RE.sub("", raw).strip()
    # Some models wrap their reply in thinking tags; never show those.
    clean = re.sub(r"<think>.*?</think>", "", clean, flags=re.DOTALL).strip()
    return clean, flagged
