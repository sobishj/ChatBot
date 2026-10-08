"""Run Alembic migrations programmatically (called on app and worker startup)."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import get_settings
from app.db.session import get_engine

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# Arbitrary constant: app and worker start at the same time; only one may migrate.
_ADVISORY_LOCK_ID = 7_301_224_001


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # '%' must be escaped for ConfigParser interpolation (e.g. in passwords).
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
    return cfg


def run_migrations() -> None:
    """Upgrade the database to the latest revision, serialised with a Postgres advisory lock."""
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": _ADVISORY_LOCK_ID})
        try:
            cfg = alembic_config()
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            conn.commit()
            logger.info("Database migrations are up to date")
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": _ADVISORY_LOCK_ID})
            conn.commit()
