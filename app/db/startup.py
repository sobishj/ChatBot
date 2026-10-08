"""Startup routine shared by the app and worker: wait for the database, then migrate."""

from __future__ import annotations

import logging
import time

from app.db.migrate import run_migrations
from app.db.session import check_database

logger = logging.getLogger(__name__)


def prepare_database(attempts: int = 30, delay_seconds: float = 2.0) -> None:
    """Block until Postgres answers, then apply migrations. Raises if it never comes up."""
    for attempt in range(1, attempts + 1):
        if check_database()["ok"]:
            run_migrations()
            return
        logger.info("Waiting for database (%s/%s)", attempt, attempts)
        time.sleep(delay_seconds)
    raise RuntimeError("Database is not reachable; cannot start")
