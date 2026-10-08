"""Turn page/document text into embedded chunks in Postgres.

Each source (a page or a document) owns its chunks: re-indexing a source deletes
its old chunks and inserts new ones in one transaction, so search never sees a
half-updated source.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.embeddings.model import Embedder
from app.indexer.chunker import chunk_text
from app.indexer.schema import current_embedding_dim, ensure_embedding_column

logger = logging.getLogger(__name__)

EMBED_BATCH = 32


@dataclass
class Source:
    """Text to index plus where it came from."""

    client_id: int
    source_type: str  # 'web' | 'doc'
    source: str  # URL or filename shown to visitors
    title: str
    text: str
    page_id: int | None = None
    document_id: int | None = None


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"


def ensure_schema(db: Session, embedder: Embedder) -> None:
    """Make sure chunks.embedding exists with the embedder's dimension."""
    if current_embedding_dim(db) != embedder.dim:
        ensure_embedding_column(db, embedder.dim)


def index_source(db: Session, embedder: Embedder, src: Source) -> int:
    """(Re)build the chunks of one page or document; returns the number of chunks. Commits."""
    if (src.page_id is None) == (src.document_id is None):
        raise ValueError("Exactly one of page_id / document_id must be set")
    pieces = chunk_text(src.text, embedder.count_tokens)
    vectors: list[list[float]] = []
    for start in range(0, len(pieces), EMBED_BATCH):
        batch = pieces[start : start + EMBED_BATCH]
        # The title gives each chunk context ("ASICS" + "Second Floor").
        vectors.extend(embedder.encode([f"{src.title}\n{p}" if src.title else p for p in batch]))

    owner_col, owner_id = ("page_id", src.page_id) if src.page_id is not None else ("document_id", src.document_id)
    db.execute(text(f"DELETE FROM chunks WHERE {owner_col} = :id"), {"id": owner_id})
    if pieces:
        db.execute(
            text(
                "INSERT INTO chunks (client_id, source_type, source, page_id, document_id, chunk_index, title, "
                "content, content_hash, embedding, updated_at) VALUES (:client_id, :source_type, :source, :page_id, "
                ":document_id, :chunk_index, :title, :content, :content_hash, CAST(:embedding AS vector), now())"
            ),
            [
                {
                    "client_id": src.client_id,
                    "source_type": src.source_type,
                    "source": src.source,
                    "page_id": src.page_id,
                    "document_id": src.document_id,
                    "chunk_index": i,
                    "title": src.title or None,
                    "content": piece,
                    "content_hash": sha256(piece),
                    "embedding": vector_literal(vec),
                }
                for i, (piece, vec) in enumerate(zip(pieces, vectors, strict=True))
            ],
        )
    db.commit()
    return len(pieces)


def delete_source_chunks(db: Session, page_id: int | None = None, document_id: int | None = None) -> None:
    if page_id is not None:
        db.execute(text("DELETE FROM chunks WHERE page_id = :id"), {"id": page_id})
    if document_id is not None:
        db.execute(text("DELETE FROM chunks WHERE document_id = :id"), {"id": document_id})
