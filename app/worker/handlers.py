"""Job handlers. Each receives a :class:`JobContext` and returns a result dict."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, text

from app.crawler.service import crawl_client, reindex_pages
from app.db.models import Client
from app.documents.service import sync_documents
from app.embeddings.model import download_model, get_embedder, load_for_download_check
from app.indexer.schema import ensure_embedding_column
from app.services import jobs as job_service
from app.services.jobs import JobCancelled, JobContext
from app.services.settings import get_setting, update_setting_dict
from app.worker.runner import handler

logger = logging.getLogger(__name__)


def _client(ctx: JobContext) -> Client:
    client = ctx.db.get(Client, ctx.job.client_id) if ctx.job.client_id else None
    if client is None:
        raise ValueError("The client of this job no longer exists.")
    return client


def _cancel_flag(ctx: JobContext) -> bool:
    return bool(ctx.db.execute(text("SELECT cancel_requested FROM jobs WHERE id = :id"), {"id": ctx.job.id}).scalar())


@handler("download_embedding")
def download_embedding(ctx: JobContext) -> dict[str, Any]:
    name = str(ctx.payload["model"])
    ctx.log(f"Downloading {name} from Hugging Face")
    try:
        path = download_model(name, progress=lambda p, t: ctx.progress(p * 0.9, t), cancelled=lambda: _cancel_flag(ctx))
    except InterruptedError as exc:
        update_setting_dict(ctx.db, "embedding", {"status": _previous_status(ctx), "pending_model": None})
        ctx.db.commit()
        raise JobCancelled() from exc
    except Exception as exc:
        update_setting_dict(ctx.db, "embedding", {"status": _previous_status(ctx), "error": str(exc)[:500], "pending_model": None})
        ctx.db.commit()
        raise
    ctx.progress(92, "Loading the model to verify it")
    dim = load_for_download_check(name, path)
    ctx.log(f"Model loaded, vector size {dim}")
    recreated = ensure_embedding_column(ctx.db, dim)
    update_setting_dict(ctx.db, "embedding", {"model": name, "dim": dim, "path": path, "status": "ready", "error": None, "pending_model": None})
    ctx.db.commit()

    # Existing content must be re-embedded with the new model.
    previous_model = ctx.payload.get("previous_model")
    clients = list(ctx.db.scalars(select(Client)))
    if clients and (recreated or (previous_model and previous_model != name)):
        for client in clients:
            job_service.enqueue(ctx.db, "reindex", client.id, triggered_by="system")
        ctx.log(f"Queued re-index for {len(clients)} client(s)")
    return {"model": name, "dim": dim}


def _previous_status(ctx: JobContext) -> str:
    embedding = get_setting(ctx.db, "embedding") or {}
    return "ready" if embedding.get("path") and embedding.get("dim") else "error"


@handler("crawl")
def crawl(ctx: JobContext) -> dict[str, Any]:
    client = _client(ctx)
    embedder = get_embedder()
    ctx.log(f"Crawling {client.website_url}")
    return crawl_client(
        ctx.db,
        client,
        embedder,
        progress=ctx.progress,
        log=ctx.log,
        should_stop=lambda: _cancel_flag(ctx),
    )


@handler("index_docs")
def index_docs(ctx: JobContext) -> dict[str, Any]:
    client = _client(ctx)
    sources = tuple(ctx.payload.get("sources") or ("upload", "folder"))
    return sync_documents(
        ctx.db, client, get_embedder(), sources=sources, force=bool(ctx.payload.get("force")),
        progress=ctx.progress, log=ctx.log, should_stop=lambda: _cancel_flag(ctx),
    )


@handler("scan_folder")
def scan_folder(ctx: JobContext) -> dict[str, Any]:
    client = _client(ctx)
    return sync_documents(
        ctx.db, client, get_embedder(), sources=("folder",), progress=ctx.progress, log=ctx.log,
        should_stop=lambda: _cancel_flag(ctx),
    )


@handler("reindex")
def reindex(ctx: JobContext) -> dict[str, Any]:
    """Re-chunk and re-embed all pages (from stored text) and all documents."""
    client = _client(ctx)
    embedder = get_embedder()
    ctx.progress(5, "Re-indexing web pages")
    web_chunks = reindex_pages(ctx.db, client, embedder, log=ctx.log)
    ctx.check_cancelled()
    ctx.progress(50, "Re-indexing documents")
    docs = sync_documents(
        ctx.db, client, embedder, force=True, progress=lambda p, t: ctx.progress(50 + p / 2, t), log=ctx.log,
        should_stop=lambda: _cancel_flag(ctx),
    )
    return {"web_chunks": web_chunks, "documents": docs}

