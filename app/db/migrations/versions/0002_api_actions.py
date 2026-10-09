"""API actions: per-client API settings, action definitions, audit log, per-session action state.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def _now() -> sa.sql.elements.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.add_column("clients", sa.Column("api_actions_enabled", sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("clients", sa.Column("api_settings", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("questions", sa.Column("actions_used", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))

    op.create_table(
        "client_actions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("method", sa.String(10), nullable=False, server_default="GET"),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("parameters", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("requires_confirmation", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("response_hint", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.UniqueConstraint("client_id", "name", name="uq_client_actions_client_name"),
    )

    op.create_table(
        "action_calls",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_id", sa.BigInteger, sa.ForeignKey("questions.id", ondelete="SET NULL")),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False, server_default="widget"),
        sa.Column("action_name", sa.String(64), nullable=False),
        sa.Column("request_summary", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status_code", sa.Integer),
        sa.Column("ok", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text),
        sa.Column("response_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
    )
    op.create_index("ix_action_calls_client_created", "action_calls", ["client_id", "created_at"])

    op.create_table(
        "action_sessions",
        sa.Column("client_id", sa.Integer, sa.ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("vault_encrypted", sa.Text),
        sa.Column("pending_encrypted", sa.Text),
        sa.Column("pending_expires_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("action_sessions")
    op.drop_index("ix_action_calls_client_created", table_name="action_calls")
    op.drop_table("action_calls")
    op.drop_table("client_actions")
    op.drop_column("questions", "actions_used")
    op.drop_column("clients", "api_settings")
    op.drop_column("clients", "api_actions_enabled")
