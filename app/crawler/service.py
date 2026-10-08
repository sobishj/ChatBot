"""Crawl a client's website and keep ``pages`` + ``chunks`` in sync (used by worker and CLI)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crawler.crawler import CrawlConfig, Crawler, FetchedPage
from app.db.models import Client, Page
from app.embeddings.model import Embedder
from app.indexer.indexer import Source, ensure_schema, index_source, sha256
from app.services.clients import crawl_settings

logger = logging.getLogger(__name__)


def crawl_client(
    db: Session,
    client: Client,
    embedder: Embedder,
    progress: Callable[[float, str], None] = lambda _p, _t: None,
    log: Callable[[str], None] = logger.info,
    should_stop: Callable[[], bool] = lambda: False,
    crawler: Crawler | None = None,
) -> dict[str, Any]:
    """Crawl ``client.website_url``; index new/changed pages, skip unchanged, remove vanished ones."""
    if not client.website_url:
        raise ValueError("This client has no website URL.")
    settings = crawl_settings(client)
    config = CrawlConfig(
        start_url=client.website_url,
        max_pages=int(settings["max_pages"]),
        delay_seconds=float(settings["delay_seconds"]),
        include_patterns=list(settings["include_patterns"]),
        exclude_patterns=list(settings["exclude_patterns"]),
        extraction=str(settings.get("extraction", "auto")),
    )
    crawler = crawler or Crawler(config)
    ensure_schema(db, embedder)
    existing: dict[str, Page] = {p.url: p for p in db.scalars(select(Page).where(Page.client_id == client.id))}
    stats = {"new": 0, "updated": 0, "unchanged": 0, "removed": 0, "chunks": 0}

    def on_page(fetched: FetchedPage) -> None:
        content_hash = sha256(fetched.title + "\n" + fetched.text)
        page = existing.get(fetched.url)
        now = datetime.now(UTC)
        if page is not None and page.content_hash == content_hash:
            page.crawled_at = now
            db.commit()
            stats["unchanged"] += 1
            return
        if page is None:
            page = Page(client_id=client.id, url=fetched.url, content_hash=content_hash)
            db.add(page)
            existing[fetched.url] = page
            stats["new"] += 1
        else:
            stats["updated"] += 1
        page.title = fetched.title
        page.content = fetched.text
        page.content_hash = content_hash
        page.crawled_at = now
        db.commit()
        stats["chunks"] += index_source(
            db,
            embedder,
            Source(client.id, "web", fetched.url, fetched.title, fetched.text, page_id=page.id),
        )
        log(f"Indexed {fetched.url}")

    def on_progress(done: int, total: int, url: str) -> None:
        progress(done * 100 / max(total, 1), f"{done} of {total} pages · {url}")

    result = crawler.crawl(on_page, on_progress, should_stop, log)

    # Remove pages that are gone (404/410), and — after a complete crawl — pages no longer linked/listed.
    complete = not result.hit_limit and not should_stop()
    for url, page in list(existing.items()):
        if url in result.gone_urls or (complete and url not in result.seen_urls):
            db.delete(page)  # chunks are removed by ON DELETE CASCADE
            stats["removed"] += 1
    client.last_crawl_at = datetime.now(UTC)
    db.commit()

    summary = {
        **stats,
        "pages_crawled": result.pages,
        "failed": result.failed,
        "hit_limit": result.hit_limit,
        "used_sitemap": result.used_sitemap,
        "errors": result.errors[:50],
    }
    log(
        f"Crawl done: {stats['new']} new, {stats['updated']} updated, {stats['unchanged']} unchanged, "
        f"{stats['removed']} removed, {result.failed} failed" + (" (page limit reached)" if result.hit_limit else "")
    )
    return summary


def reindex_pages(db: Session, client: Client, embedder: Embedder, log: Callable[[str], None] = logger.info) -> int:
    """Rebuild chunks for every stored page from its saved text (no network)."""
    ensure_schema(db, embedder)
    total = 0
    for page in db.scalars(select(Page).where(Page.client_id == client.id)):
        total += index_source(db, embedder, Source(client.id, "web", page.url, page.title or "", page.content, page_id=page.id))
    log(f"Re-indexed web pages: {total} chunks")
    return total
