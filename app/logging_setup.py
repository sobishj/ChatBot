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
    # Third-party loggers that are chatty at INFO level.
    for name in ("apscheduler", "httpx", "LiteLLM", "litellm", "sentence_transformers", "huggingface_hub"):
        logging.getLogger(name).setLevel(logging.WARNING)
