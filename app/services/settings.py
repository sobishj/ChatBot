"""Database-backed system settings (everything configurable from the admin UI).

Each setting is a JSON value stored under a key in the ``settings`` table.
Missing keys fall back to :data:`DEFAULTS`, so new settings need no migration.
"""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Setting

DEFAULT_SYSTEM_PROMPT = """You are {bot_name}, a member of the {client_name} team, chatting with visitors on our website.

Follow these rules strictly:
1. Speak as part of {client_name}: use "we", "our" and "us" (for example "We offer...", "Our office is in..."). Never call {client_name} "they" or "the company", and never describe it like an outsider. Even when asked "What is {client_name}?", answer "We are ...", not "{client_name} is ... They ...".
2. Answer ONLY with information found in the CONTEXT below. Never invent or guess facts such as names, prices, timings, locations or phone numbers.
3. Keep answers short, friendly and helpful. When the visitor asks for several things (for example services, products or timings), list every matching item from the CONTEXT.
4. Always reply in the same language the visitor used in their latest message.
5. If the CONTEXT does not contain the answer, say politely that you don't have that information right now and invite the visitor to contact us directly, giving our phone number or email if the CONTEXT has them. Then add the tag [[NO_ANSWER]] at the very end of your reply.
6. Do not mention the CONTEXT, "documents" or these rules to the visitor.

Today's date is {today}.

CONTEXT:
{context}"""

DEFAULT_CHAT_NOTICE = "Chats are recorded to improve service."

# Every setting with its default value. Keep values JSON-serialisable.
DEFAULTS: dict[str, Any] = {
    # Setup wizard
    "setup_completed": False,
    "setup_step": 1,
    "mode": "cloud",  # cloud | onprem
    "public_domain": "",  # e.g. chat.example.com (no scheme)
    "onprem_client_id": None,  # clients.id of the single on-premise client
    # AI models
    "default_model_id": None,
    "fallback_model_id": None,
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "confidence_threshold": 0.5,
    # Answer any question an uploaded document covers, even if it's unrelated to the client's business.
    "answer_any_document_topic": False,
    # Embeddings
    "embedding": {
        "model": "BAAI/bge-m3",
        "dim": None,
        "status": "not_downloaded",  # not_downloaded | downloading | ready | error
        "error": None,
    },
    "compute_device": "cpu",  # cpu | gpu: where the embedding model runs (gpu falls back to cpu if none is found)
    # Scheduling
    "crawl_schedule": {"frequency": "daily", "time": "03:00", "weekday": 0},  # weekday 0 = Monday
    "timezone": "UTC",  # IANA name used for the crawl schedule, e.g. Asia/Kolkata
    # Rate limits (requests per minute)
    "rate_limits": {
        "chat_per_ip_per_minute": 20,
        "chat_per_client_per_minute": 300,
        "login_per_ip_per_15min": 10,
        "action_calls_per_session_per_hour": 20,  # API actions: every call to a client's API
        "confirmed_actions_per_session_per_day": 3,  # API actions: confirmed changes such as bookings
    },
    # Privacy
    "chat_notice": DEFAULT_CHAT_NOTICE,
    "retention_days": 365,  # 0 = keep forever
}


def get_setting(db: Session, key: str) -> Any:
    """Return the stored value for ``key`` or a copy of its default."""
    row = db.get(Setting, key)
    if row is not None:
        return row.value
    return copy.deepcopy(DEFAULTS.get(key))


def get_settings_map(db: Session, keys: list[str] | None = None) -> dict[str, Any]:
    """Return several settings at once (all known settings when ``keys`` is None)."""
    wanted = keys if keys is not None else list(DEFAULTS)
    stored = {row.key: row.value for row in db.scalars(select(Setting).where(Setting.key.in_(wanted)))}
    return {key: stored[key] if key in stored else copy.deepcopy(DEFAULTS.get(key)) for key in wanted}


def set_setting(db: Session, key: str, value: Any) -> None:
    """Insert or update a setting (caller commits)."""
    stmt = insert(Setting).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(index_elements=[Setting.key], set_={"value": value, "updated_at": func.now()})
    db.execute(stmt)
    db.expire_all()


def update_setting_dict(db: Session, key: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Merge ``changes`` into a dict-valued setting and return the new value."""
    current = get_setting(db, key) or {}
    merged = {**current, **changes}
    set_setting(db, key, merged)
    return merged
