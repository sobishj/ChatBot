"""Runtime management of ``chunks.embedding`` (dimension depends on the embedding model)."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

HNSW_INDEX = "ix_chunks_embedding_hnsw"


def current_embedding_dim(db: Session) -> int | None:
    """Return the dimension of ``chunks.embedding``, or None if the column doesn't exist yet."""
    row = db.execute(
        text(
            "SELECT a.atttypmod FROM pg_attribute a JOIN pg_class c ON a.attrelid = c.oid "
            "WHERE c.relname = 'chunks' AND a.attname = 'embedding' AND NOT a.attisdropped"
        )
    ).first()
    return int(row[0]) if row is not None and row[0] > 0 else None


def ensure_embedding_column(db: Session, dim: int) -> bool:
    """Create (or recreate with a new size) the vector column and its HNSW index.

    Returns True when the column was (re)created, meaning all existing chunks lost
    their embeddings and the content must be re-indexed.
    """
    if dim <= 0 or dim > 16000:
        raise ValueError(f"Invalid embedding dimension: {dim}")
    current = current_embedding_dim(db)
    if current == dim:
        return False
    if current is not None:
        logger.warning("Embedding dimension changes from %s to %s: dropping old vectors", current, dim)
        db.execute(text(f"DROP INDEX IF EXISTS {HNSW_INDEX}"))
        db.execute(text("ALTER TABLE chunks DROP COLUMN embedding"))
        db.execute(text("DELETE FROM chunks"))  # chunks without vectors are useless; re-index rebuilds them
    db.execute(text(f"ALTER TABLE chunks ADD COLUMN embedding vector({int(dim)})"))
    # HNSW supports up to 2000 dimensions for the vector type.
    if dim <= 2000:
        db.execute(
            text(f"CREATE INDEX IF NOT EXISTS {HNSW_INDEX} ON chunks USING hnsw (embedding vector_cosine_ops)")
        )
    db.commit()
    return True
