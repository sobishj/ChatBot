"""Branding: chat window sizes and the per-client time zone."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.chat.service import local_now
from app.services.clients import DEFAULT_BRANDING, ClientError, public_config, validate_branding


def test_size_defaults() -> None:
    assert (DEFAULT_BRANDING["width_pct"], DEFAULT_BRANDING["height_pct"]) == (25, 70)
    assert (DEFAULT_BRANDING["mobile_width_pct"], DEFAULT_BRANDING["mobile_height_pct"]) == (92, 80)
    assert DEFAULT_BRANDING["timezone"] == ""


def test_sizes_are_whole_numbers_between_20_and_100() -> None:
    assert validate_branding({"width_pct": "40", "mobile_height_pct": 99.6}) == {"width_pct": 40, "mobile_height_pct": 100}
    for bad in (19, 101, "abc", None, float("inf")):
        with pytest.raises(ClientError, match="between 20% and 100%"):
            validate_branding({"height_pct": bad})


def test_time_zone_validation() -> None:
    assert validate_branding({"timezone": " Asia/Kolkata "}) == {"timezone": "Asia/Kolkata"}
    assert validate_branding({"timezone": ""}) == {"timezone": ""}
    with pytest.raises(ClientError, match="Unknown time zone"):
        validate_branding({"timezone": "Mars/Base"})


def test_public_config_has_sizes_but_not_time_zone() -> None:
    client = SimpleNamespace(client_id="abc", name="Acme", branding={"timezone": "Asia/Kolkata", "width_pct": 30})
    config = public_config(client, "", None)  # type: ignore[arg-type]
    assert config["width_pct"] == 30 and config["mobile_height_pct"] == 80
    assert "timezone" not in config


def test_local_now_prefers_client_zone() -> None:
    assert str(local_now("Asia/Kolkata", "Europe/London").tzinfo) == "Asia/Kolkata"
    assert str(local_now("", "Europe/London").tzinfo) == "Europe/London"
    assert local_now(None, None).utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert str(local_now("Bad/Zone", "Asia/Tokyo").tzinfo) == "Asia/Tokyo"
