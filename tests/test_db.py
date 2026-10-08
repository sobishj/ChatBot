"""Database schema, migrations and settings service tests (need Postgres)."""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.db.migrate import run_migrations
from app.db.models import Base, Chunk, Client, Page
from app.db.schema_filter import include_object as _include_object
from app.db.session import get_engine
from app.services.settings import DEFAULTS, get_setting, get_settings_map, set_setting, update_setting_dict

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "settings",
    "users",
    "user_clients",
    "ai_models",
    "clients",
    "pages",
    "documents",
    "chunks",
    "questions",
    "jobs",
    "rate_limits",
}


def test_all_tables_created(live_db: None) -> None:
    tables = set(inspect(get_engine()).get_table_names())
    assert EXPECTED_TABLES <= tables


def test_migrations_are_idempotent(live_db: None) -> None:
    run_migrations()  # second run must be a no-op
    run_migrations()


def test_models_match_migrations(live_db: None) -> None:
    """The ORM models and the migration scripts describe the same schema."""
    with get_engine().connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"include_object": _include_object, "compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_pgvector_extension_installed(live_db: None) -> None:
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar() == 1


def test_expected_indexes(live_db: None) -> None:
    insp = inspect(get_engine())
    chunk_indexes = {ix["name"] for ix in insp.get_indexes("chunks")}
    assert {"ix_chunks_client_id", "ix_chunks_content_tsv"} <= chunk_indexes
    question_indexes = {ix["name"] for ix in insp.get_indexes("questions")}
    assert "ix_questions_client_created" in question_indexes


def _client(db: Session, slug: str = "acme") -> Client:
    client = Client(client_id=slug, name=slug.title(), allowed_domains=[], branding={}, crawl_settings={}, document_settings={})
    db.add(client)
    db.flush()
    return client


def test_tsvector_is_generated_with_simple_config(db: Session) -> None:
    client = _client(db)
    db.add(Chunk(client_id=client.id, source_type="web", source="https://x", title="Shoes", content="ASICS store on the Second Floor", content_hash="h"))
    db.commit()

    hit = db.execute(
        text("SELECT count(*) FROM chunks WHERE content_tsv @@ to_tsquery('simple', 'asics')")
    ).scalar()
    miss = db.execute(
        text("SELECT count(*) FROM chunks WHERE content_tsv @@ to_tsquery('simple', 'basics')")
    ).scalar()
    assert (hit, miss) == (1, 0)


def test_deleting_client_cascades(db: Session) -> None:
    client = _client(db)
    page = Page(client_id=client.id, url="https://x/a", title="A", content="text", content_hash="h")
    db.add(page)
    db.flush()
    db.add(Chunk(client_id=client.id, source_type="web", source=page.url, page_id=page.id, content="text", content_hash="h"))
    db.commit()

    db.delete(client)
    db.commit()

    assert db.scalar(select(Page).limit(1)) is None
    assert db.scalar(select(Chunk).limit(1)) is None


def test_deleting_page_removes_its_chunks(db: Session) -> None:
    client = _client(db)
    page = Page(client_id=client.id, url="https://x/a", content="t", content_hash="h")
    db.add(page)
    db.flush()
    db.add(Chunk(client_id=client.id, source_type="web", source=page.url, page_id=page.id, content="t", content_hash="h"))
    db.commit()

    db.delete(page)
    db.commit()

    assert db.scalar(select(Chunk).limit(1)) is None


# ---------------------------------------------------------------- settings service
def test_settings_default_and_roundtrip(db: Session) -> None:
    assert get_setting(db, "mode") == DEFAULTS["mode"]

    set_setting(db, "mode", "onprem")
    db.commit()
    assert get_setting(db, "mode") == "onprem"

    set_setting(db, "mode", "cloud")  # upsert
    db.commit()
    assert get_setting(db, "mode") == "cloud"


def test_settings_defaults_are_copies(db: Session) -> None:
    value = get_setting(db, "rate_limits")
    value["chat_per_ip_per_minute"] = 1
    assert get_setting(db, "rate_limits")["chat_per_ip_per_minute"] == DEFAULTS["rate_limits"]["chat_per_ip_per_minute"]


def test_update_setting_dict_merges(db: Session) -> None:
    update_setting_dict(db, "crawl_schedule", {"time": "05:30"})
    db.commit()
    schedule = get_setting(db, "crawl_schedule")
    assert schedule["time"] == "05:30"
    assert schedule["frequency"] == DEFAULTS["crawl_schedule"]["frequency"]


def test_settings_map(db: Session) -> None:
    set_setting(db, "public_domain", "chat.example.com")
    db.commit()
    values = get_settings_map(db, ["public_domain", "mode"])
    assert values == {"public_domain": "chat.example.com", "mode": DEFAULTS["mode"]}
