"""Per-conversation action state, encrypted at rest (it holds personal data).

* The *vault* maps placeholders the model sees (``[phone_1]``) to the visitor's real values.
* A *pending* action waits for the visitor's explicit confirmation (10-minute expiry).
  Only the server executes it, and only after a clear yes: the model can't skip this step.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import ActionSession, ClientAction
from app.security.crypto import decrypt_secret, encrypt_secret

PENDING_TTL = timedelta(minutes=10)

# A clear yes or no is the whole message (after removing punctuation), not a word inside a sentence.
_YES = {
    "yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed", "correct", "go ahead",
    "yes please", "please do", "book it", "do it", "proceed", "yes book it", "yes confirm",
    "അതെ", "ശരി", "ഉവ്വ്", "ആം", "ഓക്കെ", "ശരി ബുക്ക് ചെയ്യൂ",
    "हाँ", "हां", "हा", "जी हाँ", "जी हां", "जी", "ठीक है", "हाँ जी", "haan", "han", "ha", "ji", "theek hai", "pakka",
}
_NO = {
    "no", "n", "nope", "cancel", "stop", "dont", "don't", "do not", "no thanks", "not now", "never mind", "nevermind",
    "വേണ്ട", "ഇല്ല", "റദ്ദാക്കുക",
    "नहीं", "नही", "ना", "मत करो", "रद्द करें", "nahi", "nahin", "na",
}
_STRIP_RE = re.compile(r"[^\w\s'ऀ-ॿഀ-ൿ]+")


def _normalize(text: str) -> str:
    return " ".join(_STRIP_RE.sub(" ", text.lower()).split())


def is_yes(text: str) -> bool:
    return _normalize(text) in _YES


def is_no(text: str) -> bool:
    return _normalize(text) in _NO


# ----------------------------------------------------------------------------- storage
def _state(db: Session, client_id: int, session_id: str, create: bool = False) -> ActionSession | None:
    row = db.get(ActionSession, (client_id, session_id[:64]))
    if row is None and create:
        row = ActionSession(client_id=client_id, session_id=session_id[:64])
        db.add(row)
    return row


def _load(token: str | None) -> Any:
    if not token:
        return None
    plain = decrypt_secret(token)
    return json.loads(plain) if plain else None


def load_vault(db: Session, client_id: int, session_id: str) -> dict[str, str]:
    row = _state(db, client_id, session_id)
    return dict(_load(row.vault_encrypted) or {}) if row else {}


def save_vault(db: Session, client_id: int, session_id: str, vault: dict[str, str]) -> None:
    if not vault:
        return
    row = _state(db, client_id, session_id, create=True)
    assert row is not None
    row.vault_encrypted = encrypt_secret(json.dumps(vault, ensure_ascii=False))
    row.updated_at = datetime.now(UTC)
    db.flush()


def get_pending(db: Session, client_id: int, session_id: str) -> dict[str, Any] | None:
    """The action waiting for confirmation, or None (expired ones are dropped)."""
    row = _state(db, client_id, session_id)
    if row is None or not row.pending_encrypted:
        return None
    if row.pending_expires_at is None or row.pending_expires_at < datetime.now(UTC):
        clear_pending(db, client_id, session_id)
        return None
    return _load(row.pending_encrypted)


def set_pending(db: Session, client_id: int, session_id: str, action: str, args: dict[str, Any], summary: str) -> None:
    row = _state(db, client_id, session_id, create=True)
    assert row is not None
    row.pending_encrypted = encrypt_secret(json.dumps({"action": action, "args": args, "summary": summary}, ensure_ascii=False))
    row.pending_expires_at = datetime.now(UTC) + PENDING_TTL
    db.flush()


def clear_pending(db: Session, client_id: int, session_id: str) -> None:
    row = _state(db, client_id, session_id)
    if row is not None and row.pending_encrypted:
        row.pending_encrypted = None
        row.pending_expires_at = None
        db.flush()


# ----------------------------------------------------------------------------- confirmation text
def _partly_hidden(kind: str, value: str) -> str:
    """Enough for the visitor to recognise their details, without repeating them in full."""
    if kind == "phone":
        digits = re.sub(r"\D", "", value)
        return digits[:2] + "x" * max(len(digits) - 4, 3) + digits[-2:] if len(digits) > 6 else "x" * len(digits)
    if kind == "email" and "@" in value:
        local, domain = value.split("@", 1)
        return f"{local[:1]}***@{domain}"
    return value


def confirmation_summary(action: ClientAction, params: dict[str, Any]) -> str:
    """Plain-language summary written by the server (not the model) from the validated parameters."""
    kinds = {p["name"]: p.get("type", "string") for p in action.parameters or []}
    order = [p["name"] for p in action.parameters or [] if p["name"] in params]
    details = ", ".join(f"{name.replace('_', ' ')}: {_partly_hidden(kinds.get(name, 'string'), str(params[name]))}" for name in order)
    title = action.name.replace("_", " ").capitalize()
    return f"Please confirm: {title}{' (' + details + ')' if details else ''}. Reply yes to confirm or no to cancel."
