"""Encrypt secrets (AI provider API keys) at rest with a key derived from SECRET_KEY."""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


@lru_cache
def _fernet(secret_key: str) -> Fernet:
    # Domain-separated derivation so the session-signing key and this key differ.
    digest = hashlib.sha256(b"website-assistant:api-keys:" + secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    return _fernet(get_settings().secret_key).encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str | None:
    """Return the plaintext, or None if it can't be decrypted (e.g. SECRET_KEY changed)."""
    try:
        return _fernet(get_settings().secret_key).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def mask_secret(plaintext: str | None) -> str:
    """Show only the last 4 characters, e.g. ``••••abcd``."""
    if not plaintext:
        return ""
    return "••••" + plaintext[-4:] if len(plaintext) > 8 else "••••"
