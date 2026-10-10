"""Build the LLM messages: system prompt (with retrieved context), recent history, question."""

from __future__ import annotations

import re
from datetime import date, datetime

from app.search.hybrid import SearchHit

NO_ANSWER_TAG = "[[NO_ANSWER]]"
_NO_ANSWER_RE = re.compile(r"\[\[\s*NO[_ ]ANSWER\s*\]\]", re.IGNORECASE)
MAX_CONTEXT_CHARS = 12_000


def format_context(hits: list[SearchHit], key_facts: str = "", client_name: str = "") -> str:
    """Numbered context blocks with their source, trimmed to a safe total size.

    ``key_facts`` (the client's always-included key information) comes first, as block [0].
    """
    blocks: list[str] = []
    if key_facts.strip():
        blocks.append(f"[0] Key information about {client_name or 'us'} (always applies)\n{key_facts.strip()}")
    if not hits:
        return blocks[0] if blocks else "(no relevant information found)"
    used = len(blocks[0]) if blocks else 0
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


DOCUMENTS_ANY_TOPIC_RULE = (
    "ADDITIONAL RULE: Besides {client_name}'s own information, the CONTEXT may include uploaded documents (sources marked "
    "\"document:\") on other subjects, for example another product's manual or a letter. Answer any question these documents "
    "cover, even if the subject is unrelated to {client_name}: summarise what they say about it, including partial information "
    "(for example what a product or system does), instead of saying you have no information. For such subjects, explain the "
    "documents' content neutrally instead of speaking as {client_name}, and don't call the subject another company. "
    "When the documents describe the subject, answer directly: don't begin with a disclaimer such as \"I don't have "
    "information about this\" or \"this isn't related to us\". Never add facts that are not in the CONTEXT."
)
SUBJECT_RULE = (
    "SUBJECT RULE: When the visitor doesn't say which shop, product or service they mean and it isn't clear from the "
    "conversation, they mean {client_name} itself: for example \"is it open?\" asks about {client_name}'s own opening hours, "
    "not a shop's. Only give details of an individual shop, product or service when the visitor asks about it."
)
ON_TOPIC_RULE = (
    "TOPIC RULE: Only answer questions about {client_name}: our products, services, offers, locations, opening hours, "
    "policies, contact details and other things a visitor would ask us. If the question is about something unrelated to "
    "{client_name}'s business, politely say you can only help with questions about {client_name}, even if the CONTEXT "
    "happens to contain related text (for example from an uploaded document), and add the tag [[NO_ANSWER]] at the very end."
)


def build_messages(
    template: str,
    bot_name: str,
    client_name: str,
    hits: list[SearchHit],
    history: list[tuple[str, str]],
    question: str,
    documents_any_topic: bool = False,
    key_facts: str = "",
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """``history`` is a list of (question, answer) pairs, oldest first.

    ``documents_any_topic`` (the client's Documents tab) lets uploaded documents answer questions on any
    subject; otherwise the assistant keeps to the client's business.
    """
    context = format_context(hits, key_facts, client_name)
    system = render_system_prompt(template, bot_name, client_name, context, now.date() if now else None)
    rule = DOCUMENTS_ANY_TOPIC_RULE if documents_any_topic else ON_TOPIC_RULE
    system += "\n\n" + SUBJECT_RULE.format(client_name=client_name) + "\n\n" + rule.format(client_name=client_name)
    if now is not None:  # lets the assistant answer "is it open now?"
        system += f"\n\nCurrent local time: {now.strftime('%A, %d %B %Y, %H:%M')} ({now.tzname() or 'local'})."
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
