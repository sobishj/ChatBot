"""Branding: chat window sizes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.clients import DEFAULT_BRANDING, ClientError, public_config, validate_branding


def test_size_defaults() -> None:
    assert (DEFAULT_BRANDING["width_pct"], DEFAULT_BRANDING["height_pct"]) == (25, 70)
    assert (DEFAULT_BRANDING["mobile_width_pct"], DEFAULT_BRANDING["mobile_height_pct"]) == (92, 80)


def test_sizes_are_whole_numbers_between_20_and_100() -> None:
    assert validate_branding({"width_pct": "40", "mobile_height_pct": 99.6}) == {"width_pct": 40, "mobile_height_pct": 100}
    for bad in (19, 101, "abc", None, float("inf")):
        with pytest.raises(ClientError, match="between 20% and 100%"):
            validate_branding({"height_pct": bad})


def test_public_config_has_sizes() -> None:
    client = SimpleNamespace(client_id="abc", name="Acme", branding={"width_pct": 30})
    config = public_config(client, "", None)  # type: ignore[arg-type]
    assert config["width_pct"] == 30 and config["mobile_height_pct"] == 80
