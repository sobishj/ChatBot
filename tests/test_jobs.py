"""Job queue, worker execution and the crawl service (Postgres)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.crawler.crawler import CrawlConfig, Crawler
from app.crawler.service import crawl_client
from app.db.models import Chunk, Job, Page
from app.services import jobs as job_service
from app.worker import runner

pytestmark = pytest.mark.integration


def test_enqueue_dedupes_and_claim(db: Session, make_client) -> None:
    client = make_client()
    a = job_service.enqueue(db, "crawl", client.id)
    b = job_service.enqueue(db, "crawl", client.id)
    assert a.id == b.id
    claimed = job_service.claim_next(db)
    assert claimed.id == a.id and claimed.status == "running"
    assert job_service.claim_next(db) is None


def test_run_job_success_failure_and_cancel(db: Session, make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client()
    calls: list[str] = []

    def ok(ctx):
        ctx.progress(50, "halfway")
        ctx.log("working")
        calls.append("ok")
        return {"pages": 3}

    def boom(ctx):
        raise RuntimeError("site down")

    def cancellable(ctx):
        db.execute(text("UPDATE jobs SET cancel_requested = true WHERE id = :id"), {"id": ctx.job.id})
        db.commit()
        ctx.check_cancelled()

    monkeypatch.setitem(runner.HANDLERS, "crawl", ok)
    job = job_service.enqueue(db, "crawl", client.id)
    job_service.claim_next(db)
    assert runner.run_job(job.id) == "succeeded"
    db.refresh(job)
    assert job.status == "succeeded" and job.progress == 100 and job.result == {"pages": 3} and "working" in job.log

    monkeypatch.setitem(runner.HANDLERS, "index_docs", boom)
    job = job_service.enqueue(db, "index_docs", client.id)
    job_service.claim_next(db)
    assert runner.run_job(job.id) == "failed"
    db.refresh(job)
    assert job.error == "site down" and "ERROR" in job.log

    retry = job_service.retry(db, job)
    assert retry.id != job.id and retry.status == "queued"

    monkeypatch.setitem(runner.HANDLERS, "scan_folder", cancellable)
    job = job_service.enqueue(db, "scan_folder", client.id)
    job_service.claim_next(db)
    assert runner.run_job(job.id) == "cancelled"


def test_cancel_queued_and_recover_stale(db: Session, make_client) -> None:
    client = make_client()
    job = job_service.enqueue(db, "crawl", client.id)
    job_service.request_cancel(db, job)
    assert job.status == "cancelled"

    # Indexing-type jobs interrupted by a worker restart run again, up to MAX_ATTEMPTS times.
    stale = job_service.enqueue(db, "reindex", client.id)
    for attempt in range(1, job_service.MAX_ATTEMPTS + 1):
        assert job_service.claim_next(db).id == stale.id
        db.execute(text("UPDATE jobs SET heartbeat_at = :t WHERE id = :id"), {"t": datetime.now(UTC) - timedelta(minutes=10), "id": stale.id})
        db.commit()
        assert job_service.recover_stale_jobs(db) == 1
        db.refresh(stale)
        if attempt < job_service.MAX_ATTEMPTS:
            assert stale.status == "queued" and stale.payload["_attempts"] == attempt + 1 and "running it again" in stale.log
    assert stale.status == "failed"

    # Other jobs (e.g. a model download) are not repeated automatically.
    download = job_service.enqueue(db, "download_embedding", payload={"model": "x/y"})
    job_service.claim_next(db)
    db.execute(text("UPDATE jobs SET heartbeat_at = NULL WHERE id = :id"), {"id": download.id})
    db.commit()
    job_service.recover_stale_jobs(db)
    db.refresh(download)
    assert download.status == "failed"


def test_worker_heartbeat_only_keeps_its_own_jobs_alive(db: Session, make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.worker import runner

    client = make_client()
    mine = job_service.enqueue(db, "crawl", client.id)
    orphan = job_service.enqueue(db, "index_docs", client.id)
    job_service.claim_next(db)
    job_service.claim_next(db)
    old = datetime.now(UTC) - timedelta(minutes=10)
    db.execute(text("UPDATE jobs SET heartbeat_at = :t"), {"t": old})
    db.commit()

    worker = runner.Worker(threads=1)
    worker._running.add(mine.id)
    monkeypatch.setattr(runner, "RECOVER_EVERY", 1)
    monkeypatch.setattr(worker._stop, "wait", lambda _s: worker._stop.set())  # one heartbeat, then stop
    worker._heartbeat()
    db.expire_all()
    assert db.get(job_service.Job, mine.id).status == "running"
    assert db.get(job_service.Job, orphan.id).status == "queued"  # orphaned by a restart: queued again


def _site(pages: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        if request.url.path in pages:
            return httpx.Response(200, text=pages[request.url.path], headers={"content-type": "text/html"})
        return httpx.Response(404, text="")

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _html(title: str, body: str, links: str = "") -> str:
    return f"<html><head><title>{title}</title></head><body><main>{body}{links}</main></body></html>"


def test_crawl_service_skips_unchanged_and_removes_vanished(db: Session, embedder, make_client) -> None:
    client = make_client("site", "Site", "https://www.site.test")
    v1 = {"/": _html("Home", "<p>Welcome</p>", '<a href="/a/">a</a><a href="/b/">b</a>'), "/a/": _html("A", "<p>Alpha</p>"), "/b/": _html("B", "<p>Beta</p>")}

    def run(pages):
        crawler = Crawler(CrawlConfig(start_url="https://www.site.test/", delay_seconds=0), client=_site(pages), sleep=lambda s: None)
        return crawl_client(db, client, embedder, crawler=crawler)

    first = run(v1)
    assert first["new"] == 3 and first["removed"] == 0
    assert db.scalar(select(Chunk).where(Chunk.client_id == client.id)) is not None

    second = run(v1)
    assert second["unchanged"] == 3 and second["new"] == 0

    v2 = {"/": _html("Home", "<p>Welcome</p>", '<a href="/a/">a</a>'), "/a/": _html("A", "<p>Alpha changed</p>")}
    third = run(v2)
    assert third["updated"] == 2 and third["removed"] == 1  # home (its links changed) + /a/
    urls = set(db.scalars(select(Page.url).where(Page.client_id == client.id)))
    assert urls == {"https://www.site.test/", "https://www.site.test/a/"}
    assert client.last_crawl_at is not None
    assert db.scalar(select(Job).limit(1)) is None  # service itself doesn't enqueue jobs
