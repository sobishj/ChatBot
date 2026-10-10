"""Hybrid retrieval for one client.

1. Vector search: top 20 chunks by cosine similarity (pgvector HNSW index).
2. Full-text search: top 20 chunks by ``ts_rank_cd`` on the ``simple`` tsvector. Query
   words are OR-ed (minus common stop words) so a question like "Where is the ASICS
   store?" matches chunks containing "asics" — and, because ``simple`` matches whole
   words only, never "Basics".
3. Reciprocal rank fusion: score = Σ 1 / (k + rank) over both lists; keep the top 6.

The confidence score is the best cosine similarity among the returned chunks (RRF
scores are rank-based and not comparable across questions). Every query is filtered
by ``client_id``.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.embeddings.model import Embedder, encode_queries
from app.indexer.indexer import vector_literal

VECTOR_K = 20
TEXT_K = 20
FINAL_K = 6
RRF_K = 60

# Very common words that carry no meaning for keyword search (English + a few others).
# Other languages simply keep all their words; the vector search handles meaning.
STOP_WORDS = set(
    """a an the is are was were be been am do does did of to in on at by for with from and or not no
    what where when which who whom whose why how can could would should will shall may might must
    i me my we our you your he she it its they them their this that these those there here
    please tell show give get find know want need any some about have has had
    le la les un une des du de et est que qui où quand comment el los las y es en por para
    der die das und ist wo wie""".split()
)


def words(text_: str) -> list[str]:
    """Split on whitespace/punctuation/symbols only, so combining marks (e.g. Malayalam
    vowel signs) stay inside their word, unlike word matching in Python regexes."""
    cleaned = "".join(" " if unicodedata.category(ch)[0] in "PSZC" else ch for ch in text_)
    return cleaned.lower().split()


@dataclass
class SearchHit:
    chunk_id: int
    source_type: str
    source: str
    title: str
    content: str
    similarity: float | None = None  # cosine similarity (vector list only)
    text_rank: float | None = None
    score: float = 0.0  # RRF score


@dataclass
class SearchResult:
    hits: list[SearchHit] = field(default_factory=list)
    confidence: float = 0.0  # best cosine similarity among hits (0..1)
    keyword_match: bool = False  # at least one hit came from full-text search


def keyword_query(question: str) -> str | None:
    """Build an OR tsquery string from meaningful words, e.g. 'asics | store'."""
    terms = [w for w in words(question) if w not in STOP_WORDS and (len(w) > 1 or not w.isascii())]
    terms = list(dict.fromkeys(terms))[:20]
    if not terms:
        return None
    # Each term is quoted so tsquery operators in user input are treated literally.
    return " | ".join("'" + t.replace("'", "''").replace("\\", "") + "'" for t in terms)


# Chunks of documents switched off in the Documents tab are never used for answers.
_ENABLED_ONLY = "AND (document_id IS NULL OR document_id NOT IN (SELECT id FROM documents WHERE client_id = :cid AND NOT enabled)) "


def _vector_search(db: Session, client_id: int, query_vector: list[float], k: int) -> list[SearchHit]:
    try:
        # pgvector >= 0.8: keep scanning the HNSW index until enough rows pass the client filter.
        db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    except DBAPIError:
        db.rollback()
    db.execute(text("SET LOCAL hnsw.ef_search = 100"))
    rows = db.execute(
        text(
            "SELECT id, source_type, source, coalesce(title, ''), content, "
            "1 - (embedding <=> CAST(:q AS vector)) AS similarity "
            "FROM chunks WHERE client_id = :cid AND embedding IS NOT NULL "
            + _ENABLED_ONLY
            + "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
        ),
        {"q": vector_literal(query_vector), "cid": client_id, "k": k},
    ).all()
    return [SearchHit(r[0], r[1], r[2], r[3], r[4], similarity=float(r[5])) for r in rows]


def _text_search(db: Session, client_id: int, question: str, k: int) -> list[SearchHit]:
    query = keyword_query(question)
    if query is None:
        return []
    rows = db.execute(
        text(
            "SELECT id, source_type, source, coalesce(title, ''), content, "
            "ts_rank_cd(content_tsv, q, 32) AS rank "
            "FROM chunks, to_tsquery('simple', :q) AS q "
            "WHERE client_id = :cid AND content_tsv @@ q "
            + _ENABLED_ONLY
            + "ORDER BY rank DESC, id LIMIT :k"
        ),
        {"q": query, "cid": client_id, "k": k},
    ).all()
    return [SearchHit(r[0], r[1], r[2], r[3], r[4], text_rank=float(r[5])) for r in rows]


def rrf_merge(lists: list[list[SearchHit]], k: int = RRF_K, limit: int = FINAL_K) -> list[SearchHit]:
    """Reciprocal rank fusion of several ranked lists (deduplicated by chunk id)."""
    merged: dict[int, SearchHit] = {}
    for hits in lists:
        for rank, hit in enumerate(hits, start=1):
            current = merged.get(hit.chunk_id)
            if current is None:
                current = SearchHit(hit.chunk_id, hit.source_type, hit.source, hit.title, hit.content)
                merged[hit.chunk_id] = current
            current.score += 1.0 / (k + rank)
            if hit.similarity is not None:
                current.similarity = hit.similarity
            if hit.text_rank is not None:
                current.text_rank = hit.text_rank
    return sorted(merged.values(), key=lambda h: h.score, reverse=True)[:limit]


def search(db: Session, embedder: Embedder, client_id: int, question: str, limit: int = FINAL_K) -> SearchResult:
    query_vector = encode_queries(embedder, [question])[0]
    vector_hits = _vector_search(db, client_id, query_vector, VECTOR_K)
    text_hits = _text_search(db, client_id, question, TEXT_K)
    db.commit()  # end the transaction holding SET LOCAL
    hits = rrf_merge([vector_hits, text_hits], limit=limit)
    # Chunks found only by keywords have no similarity yet; the best vector similarity
    # among the final hits is the confidence.
    sims = [h.similarity for h in hits if h.similarity is not None]
    return SearchResult(
        hits=hits,
        confidence=max(sims) if sims else 0.0,
        keyword_match=any(h.text_rank is not None for h in hits),
    )
