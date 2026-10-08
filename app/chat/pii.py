"""Mask personal data (email addresses, phone numbers) in visitor messages.

Applied before a message is searched, sent to the model or saved, so visitor
contact details never leave the request in clear text.
"""

from __future__ import annotations

import re

EMAIL_MASK = "[email]"
PHONE_MASK = "[phone]"

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.UNICODE)
# Digits with optional +, spaces, dots, dashes, parentheses between them.
_PHONE_CANDIDATE_RE = re.compile(r"(?<![\w+])\+?\(?\d[\d\s().\-/]{5,}\d(?!\w)")
_DATE_RE = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$|^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")
_TIME_RANGE_RE = re.compile(r"^\d{1,2}[.:]\d{2}\s*-\s*\d{1,2}[.:]\d{2}$")


def _mask_phone(match: re.Match[str]) -> str:
    candidate = match.group(0)
    digits = sum(ch.isdigit() for ch in candidate)
    stripped = candidate.strip()
    if digits < 7 or digits > 15 or _DATE_RE.match(stripped) or _TIME_RANGE_RE.match(stripped):
        return candidate
    return PHONE_MASK


def mask_pii(text: str) -> str:
    """Replace email addresses and phone numbers with placeholders."""
    text = _EMAIL_RE.sub(EMAIL_MASK, text)
    return _PHONE_CANDIDATE_RE.sub(_mask_phone, text)
