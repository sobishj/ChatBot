"""Document sources (uploads + watched folder) kept in sync with ``documents`` and ``chunks``.

* Uploads live in ``DATA_DIR/clients/<client_id>/documents/``.
* A watched folder is any readable directory under WATCHED_DIR (``/mnt/watched``),
  mounted read-only from the host (network shares are mounted on the host first).

Both sources are synced the same way: new/changed files (by SHA-256) are indexed,
unchanged files are skipped, and rows for deleted files are removed (their chunks
go with them via ON DELETE CASCADE).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Client, Document
from app.documents.readers import (
    SUPPORTED_EXTENSIONS,
    EmptyDocument,
    ReadCancelled,
    UnsupportedDocument,
    is_supported,
    read_document,
)
from app.embeddings.model import Embedder
from app.indexer.indexer import Source, ensure_schema, index_source
from app.services.clients import client_data_dir, document_settings

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # scanned PDFs run to ~100-300 KB per page
SOURCES = ("upload", "folder")


class DocumentError(ValueError):
    """User-facing document error."""


# ----------------------------------------------------------------------------- paths
def uploads_dir(client: Client) -> Path:
    return client_data_dir(client.client_id) / "documents"


def safe_filename(name: str) -> str:
    """Keep letters (any script), digits, spaces, dots, dashes, underscores; drop any path parts."""
    name = unicodedata.normalize("NFC", Path(name.replace("\\", "/")).name).strip()
    # Letters, combining marks (Indic vowel signs), digits and a few safe punctuation characters.
    name = "".join(ch if unicodedata.category(ch)[0] in "LMN" or ch in " .-_()" else "_" for ch in name)
    name = re.sub(r"_+", "_", name).strip(" .")
    if not name:
        raise DocumentError("Invalid file name.")
    return name[:200]


def validate_watch_path(raw: str) -> str:
    """Resolve and check a watched-folder path; it must be a readable directory under WATCHED_DIR."""
    root = get_settings().watched_dir.resolve()
    path = Path(raw.strip())
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise DocumentError(f"The folder must be inside {root} (mounted from the host via WATCHED_HOST_DIR).")
    if not resolved.exists():
        raise DocumentError(f"Folder not found: {resolved}")
    if not resolved.is_dir():
        raise DocumentError(f"Not a folder: {resolved}")
    try:
        next(os.scandir(resolved), None)
    except PermissionError as exc:
        raise DocumentError(f"The folder is not readable: {resolved}") from exc
    return str(resolved)


def source_root(client: Client, source: str) -> Path | None:
    if source == "upload":
        return uploads_dir(client)
    watch = document_settings(client).get("watch_path")
    return Path(watch) if watch else None


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _iter_files(root: Path, recursive: bool) -> Iterator[Path]:
    if not root.is_dir():
        return
    iterator = root.rglob("*") if recursive else root.glob("*")
    for path in iterator:
        if path.is_file() and is_supported(path) and not path.name.startswith(("~$", ".")):
            yield path


# ----------------------------------------------------------------------------- uploads
def save_upload(db: Session, client: Client, filename: str, stream: BinaryIO) -> Document:
    """Store an uploaded file (streamed, size-limited) and register it as pending."""
    name = safe_filename(filename)
    if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise DocumentError(f"Unsupported file type. Allowed: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
    folder = uploads_dir(client)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    tmp = folder / f".{name}.part"
    size = 0
    try:
        with tmp.open("wb") as out:
            while block := stream.read(1024 * 1024):
                size += len(block)
                if size > MAX_UPLOAD_BYTES:
                    raise DocumentError(f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
                out.write(block)
        if size == 0:
            raise DocumentError("The file is empty.")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)

    doc = db.scalar(select(Document).where(Document.client_id == client.id, Document.source == "upload", Document.path == name))
    if doc is None:
        doc = Document(client_id=client.id, source="upload", path=name, filename=name, chunk_count=0)
        db.add(doc)
    doc.size_bytes = size
    doc.status = "pending"
    doc.error = None
    doc.uploaded_at = datetime.now(UTC)
    db.commit()
    return doc


def delete_document(db: Session, doc: Document, client: Client) -> None:
    """Delete an uploaded document (file + chunks). Folder documents are removed by deleting the file."""
    if doc.source != "upload":
        raise DocumentError("Files from the watched folder are removed by deleting them from the folder.")
    (uploads_dir(client) / doc.path).unlink(missing_ok=True)
    db.delete(doc)
    db.commit()


# ----------------------------------------------------------------------------- sync
def sync_documents(
    db: Session,
    client: Client,
    embedder: Embedder,
    sources: tuple[str, ...] = SOURCES,
    force: bool = False,
    progress: Callable[[float, str], None] = lambda _p, _t: None,
    log: Callable[[str], None] = logger.info,
    should_stop: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Index new/changed files, skip unchanged ones, drop rows of deleted files."""
    ensure_schema(db, embedder)
    stats = {"indexed": 0, "unchanged": 0, "removed": 0, "errors": 0, "chunks": 0}
    for source in sources:
        root = source_root(client, source)
        rows = {d.path: d for d in db.scalars(select(Document).where(Document.client_id == client.id, Document.source == source))}
        if root is None or not root.is_dir():
            if source == "folder" and root is not None:
                log(f"Watched folder is not available: {root}")
                continue  # keep rows: the share may be temporarily unmounted
            files: list[Path] = []
        else:
            files = list(_iter_files(root, recursive=(source == "folder")))
        present: set[str] = set()
        for i, path in enumerate(files, start=1):
            if should_stop():
                return stats
            rel = path.relative_to(root).as_posix() if root else path.name
            present.add(rel)
            label = f"{source}: {i} of {len(files)} · {rel}"
            progress((i - 1) * 100 / max(len(files), 1), label)
            doc = rows.get(rel)
            if doc is None:
                doc = Document(client_id=client.id, source=source, path=rel, filename=path.name, status="pending", chunk_count=0, size_bytes=0)
                db.add(doc)
                db.flush()
                rows[rel] = doc
            try:
                digest = file_hash(path)
                doc.size_bytes = path.stat().st_size
            except OSError as exc:
                _mark_error(db, doc, f"Cannot read file: {exc.strerror or exc}")
                stats["errors"] += 1
                continue
            if not force and doc.file_hash == digest and doc.status == "indexed":
                stats["unchanged"] += 1
                continue
            def read_progress(read: int, total: int, i: int = i, n: int = len(files), label: str = label) -> None:
                # Reading (incl. OCR) fills the first half of this file's share of the bar, embedding the second.
                progress((i - 1 + read / max(total, 1) / 2) * 100 / n, f"{label} · read page {read} of {total}")

            try:
                text = read_document(path, read_progress, should_stop)
            except ReadCancelled:
                return stats
            except (UnsupportedDocument, EmptyDocument) as exc:
                doc.file_hash = digest
                _mark_error(db, doc, str(exc))
                log(f"{rel}: {exc}")
                stats["errors"] += 1
                continue
            db.commit()

            def chunk_progress(done: int, total: int, i: int = i, n: int = len(files), label: str = label) -> None:
                progress((i - 0.5 + done / total / 2) * 100 / n, f"{label} · embedded {done} of {total} chunks")

            doc_id = doc.id
            try:
                count = index_source(
                    db,
                    embedder,
                    Source(client.id, "doc", doc.filename, Path(doc.filename).stem, text, document_id=doc_id),
                    progress=chunk_progress,
                )
            except IntegrityError:
                db.rollback()
                if db.get(Document, doc_id) is None:  # deleted in the admin UI while it was being indexed
                    log(f"Skipped {rel}: it was deleted while being indexed")
                    rows.pop(rel, None)
                    continue
                raise
            doc.file_hash = digest
            doc.status = "indexed"
            doc.error = None
            doc.chunk_count = count
            doc.indexed_at = datetime.now(UTC)
            db.commit()
            stats["indexed"] += 1
            stats["chunks"] += count
            log(f"Indexed {source} document {rel} ({count} chunks)")
        for rel, doc in rows.items():
            if rel not in present:
                db.delete(doc)
                stats["removed"] += 1
                log(f"Removed {source} document {rel} (file deleted)")
        db.commit()
    if "folder" in sources:
        settings = {**document_settings(client), "last_scan_at": datetime.now(UTC).isoformat()}
        client.document_settings = settings
        db.commit()
    return stats


def _mark_error(db: Session, doc: Document, message: str) -> None:
    from sqlalchemy import text

    db.execute(text("DELETE FROM chunks WHERE document_id = :id"), {"id": doc.id})
    doc.status = "error"
    doc.error = message[:1000]
    doc.chunk_count = 0
    db.commit()


def document_to_dict(doc: Document) -> dict[str, Any]:
    return {
        "id": doc.id,
        "source": doc.source,
        "path": doc.path,
        "filename": doc.filename,
        "size_bytes": doc.size_bytes,
        "status": doc.status,
        "error": doc.error,
        "chunk_count": doc.chunk_count,
        "uploaded_at": doc.uploaded_at,
        "indexed_at": doc.indexed_at,
    }
