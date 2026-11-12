"""Crawler picture cache: what each picture on a crawled page says, so re-crawls don't read it again.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_text_cache",
        sa.Column("url_hash", sa.Text, primary_key=True),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("image_text_cache")
