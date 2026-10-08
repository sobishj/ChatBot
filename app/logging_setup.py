"""Logging configuration shared by the app and worker processes."""

from __future__ import annotations

import logging


def configure_logging(level: str = "INFO") -> None:
    """Send all logs (including uvicorn's) to stderr in one consistent format."""
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        force=True,
    )
