"""Contact details when the assistant can't answer (ported from worker/test/contact.test.ts)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.chat.contact import contact_rule, mentions_contact, with_contact
from app.chat.prompt import build_messages
from app.services.clients import validate_branding

CONTACT = "Phone: +91 98765 43210\nEmail: info@taxlead.in"


def test_adds_the_details_to_an_answer_without_them() -> None:
    out = with_contact("I don't have that information right now.", CONTACT, "en")
    assert out == "I don't have that information right now.\n\nYou can contact us here:\n" + CONTACT


def test_does_not_repeat_details_the_model_already_gave_however_it_wrote_the_number() -> None:
    assert mentions_contact("Please call us on 98765-43210.", CONTACT)
    assert mentions_contact("Write to INFO@taxlead.in", CONTACT)
    assert with_contact("Call +91 9876543210 for help.", CONTACT, "en") == "Call +91 9876543210 for help."


def test_does_nothing_without_configured_details_and_uses_the_visitors_language() -> None:
    assert with_contact("Sorry.", "", "en") == "Sorry."
    assert with_contact("Sorry.", "   ", "en") == "Sorry."
    assert with_contact("Sorry.", None, "en") == "Sorry."
    assert "ഞങ്ങളെ ബന്ധപ്പെടാൻ:" in with_contact("ക്ഷമിക്കണം.", CONTACT, "ml")
    assert "You can contact us here:" in with_contact("Sorry.", CONTACT, None)


def test_puts_the_contact_rule_in_the_prompt() -> None:
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    with_details = build_messages("{context}", "Bot", "Acme", [], [], "hi", "", now, "", CONTACT)[0]["content"]
    assert "CONTACT RULE" in with_details
    assert "info@taxlead.in" in with_details
    assert "if the CONTEXT has our contact details" in build_messages("{context}", "Bot", "Acme", [], [], "hi", "", now)[0]["content"]
    assert "Never invent contact details" in contact_rule("")


def test_contact_details_are_capped_at_500_characters() -> None:
    assert validate_branding({"contact_details": "x" * 600}) == {"contact_details": "x" * 500}
