"""Contact details for questions the assistant can't answer.

The prompt asks the model to give them (``contact_rule``), and ``with_contact`` adds them to
the reply when the model left them out.
"""

from __future__ import annotations

import re


def contact_rule(contact: str) -> str:
    """The prompt rule; without configured details the model may still use those in the CONTEXT."""
    details = (contact or "").strip()
    if details:
        return (
            "CONTACT RULE: If you can't answer the question from the CONTEXT, say so briefly and kindly, then give the "
            f"visitor our contact details so they can ask us directly:\n{details}\nNever invent other contact details."
        )
    return (
        "CONTACT RULE: If you can't answer the question from the CONTEXT, say so briefly and kindly, and if the CONTEXT "
        "has our contact details (phone, email, WhatsApp, address), give them so the visitor can ask us directly. "
        "Never invent contact details."
    )


LEAD_IN = {
    "en": "You can contact us here:",
    "ml": "ഞങ്ങളെ ബന്ധപ്പെടാൻ:",
    "hi": "आप हमसे यहाँ संपर्क कर सकते हैं:",
    "ta": "எங்களைத் தொடர்பு கொள்ள:",
    "ar": "يمكنك التواصل معنا هنا:",
}

_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{4,}\d")
_WORD_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|https?://\S+|\bwa\.me/\S+", re.IGNORECASE)


def _contact_tokens(contact: str) -> tuple[list[str], list[str]]:
    """Phone numbers (digits only), emails and links in the contact details."""
    digits = [d for d in (re.sub(r"\D", "", m) for m in _PHONE_RE.findall(contact)) if len(d) >= 6]
    words = [w.lower() for w in _WORD_RE.findall(contact)]
    return digits, words


def mentions_contact(answer: str, contact: str) -> bool:
    """True when the answer already gives (any of) the contact details."""
    digits, words = _contact_tokens(contact)
    answer_digits = re.sub(r"\D", "", answer)
    lower = answer.lower()
    # Last 8 digits: the model may leave out the country or area code (+91 98765 43210 -> 98765 43210).
    if any(d[-8:] in answer_digits for d in digits) or any(w in lower for w in words):
        return True
    if not digits and not words:
        return contact.strip().lower()[:40] in lower
    return False


def with_contact(answer: str, contact: str | None, language: str | None) -> str:
    """The answer with the contact details added at the end, unless it already has them."""
    details = (contact or "").strip()
    if not details or mentions_contact(answer, details):
        return answer
    lead = LEAD_IN.get((language or "en")[:2], LEAD_IN["en"])
    return f"{answer.strip()}\n\n{lead}\n{details}"
