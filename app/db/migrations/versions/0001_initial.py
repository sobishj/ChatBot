"""Initial schema: settings, users, AI models, clients, content, questions, jobs, rate limits.

chunks.embedding is intentionally absent: it is created at runtime once the embedding
model (and therefore the vector dimension) is chosen. See app.indexer.schema.

Revision ID: 0001
Revises:
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def _now() -> sa.sql.elements.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", JSONB, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("session_version", sa.Integer, nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
    )

    op.create_table(
        "ai_models",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("base_url", sa.Text),
        sa.Column("model_name", sa.String(300), nullable=False),
        sa.Column("api_key_encrypted", sa.Text),
        sa.Column("temperature", sa.Float, nullable=False),
        sa.Column("max_tokens", sa.Integer, nullable=False),
        sa.Column("timeout_seconds", sa.Integer, nullable=False),
        sa.Column("cost_input_per_m", sa.Float),
        sa.Column("cost_output_per_m", sa.Float),
        sa.Column("last_test_ok", sa.Boolean),
        sa.Column("last_test_at", sa.DateTime(timezone=True)),
        sa.Column("last_test_message", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
    )

    op.create_table(
        "clients",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("client_id", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("website_url", sa.Text),
        sa.Column("allowed_domains", JSONB, nullable=False),
        sa.Column("branding", JSONB, nullable=False),
        sa.Column("crawl_settings", JSONB, nullable=False),
        sa.Column("document_settings", JSONB, nullable=False),
        sa.Column("ai_model_id", sa.Integer, sa.ForeignKey("ai_models.id", ondelete="SET NULL")),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("last_crawl_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
    )

    op.create_table(
        "user_clients",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True),
    )

    op.create_table(
        "pages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("crawled_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.UniqueConstraint("client_id", "url", name="uq_pages_client_url"),
    )
    op.create_index("ix_pages_client_id", "pages", ["client_id"])

    op.create_table(
        "documents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("file_hash", sa.String(64)),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("chunk_count", sa.Integer, nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("client_id", "source", "path", name="uq_documents_client_source_path"),
    )
    op.create_index("ix_documents_client_id", "documents", ["client_id"])

    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_type", sa.String(10), nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("page_id", sa.Integer, sa.ForeignKey("pages.id", ondelete="CASCADE")),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id", ondelete="CASCADE")),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column(
            "content_tsv",
            postgresql.TSVECTOR,
            sa.Computed("to_tsvector('simple', coalesce(title, '') || ' ' || content)", persisted=True),
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
    )
    op.create_index("ix_chunks_client_id", "chunks", ["client_id"])
    op.create_index("ix_chunks_page_id", "chunks", ["page_id"])
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.create_index("ix_chunks_content_tsv", "chunks", ["content_tsv"], postgresql_using="gin")

    op.create_table(
        "questions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("question_normalized", sa.Text, nullable=False),
        sa.Column("answer", sa.Text, nullable=False),
        sa.Column("sources", JSONB, nullable=False),
        sa.Column("answered", sa.Boolean, nullable=False),
        sa.Column("top_score", sa.Float),
        sa.Column("language", sa.String(16)),
        sa.Column("model_id", sa.Integer, sa.ForeignKey("ai_models.id", ondelete="SET NULL")),
        sa.Column("model_name", sa.String(300)),
        sa.Column("used_fallback", sa.Boolean, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column("cost", sa.Float, nullable=False),
        sa.Column("response_ms", sa.Integer, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
    )
    op.create_index("ix_questions_client_created", "questions", ["client_id", "created_at"])
    op.create_index("ix_questions_session", "questions", ["client_id", "session_id", "created_at"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE")),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("progress", sa.Integer, nullable=False),
        sa.Column("progress_text", sa.Text),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("result", JSONB),
        sa.Column("log", sa.Text, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("cancel_requested", sa.Boolean, nullable=False),
        sa.Column("triggered_by", sa.String(20), nullable=False),
        sa.Column("created_by_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_jobs_client_id", "jobs", ["client_id"])
    op.create_index("ix_jobs_status_created", "jobs", ["status", "created_at"])

    op.create_table(
        "rate_limits",
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("count", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    for table in (
        "rate_limits",
        "jobs",
        "questions",
        "chunks",
        "documents",
        "pages",
        "user_clients",
        "clients",
        "ai_models",
        "users",
        "settings",
    ):
        op.drop_table(table)
