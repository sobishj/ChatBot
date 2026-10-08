"""Objects Alembic autogenerate must ignore (shared by env.py and tests)."""

from __future__ import annotations

from typing import Any


def include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    """Skip ``chunks.embedding`` and its index: they are managed at runtime (app.indexer.schema)."""
    if type_ == "column" and name == "embedding" and getattr(obj, "table", None) is not None:
        return obj.table.name != "chunks"
    if type_ == "index" and name == "ix_chunks_embedding_hnsw":
        return False
    return True
