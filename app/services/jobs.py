"""Postgres-backed job queue.

The admin UI (and scheduler/CLI) enqueue jobs; the worker claims them with
``SELECT ... FOR UPDATE SKIP LOCKED`` so several worker threads/containers never
run the same job. Progress, logs and errors are written back to the ``jobs`` row,
which the UI polls for live progress.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Job

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running")
MAX_LOG_CHARS = 200_000

# Human-friendly names shown in the UI.
JOB_TYPES = {
    "crawl": "Website crawl",
    "index_docs": "Index documents",
    "scan_folder": "Scan watched folder",
    "reindex": "Re-index everything",
    "download_embedding": "Download embedding model",
}


class JobCancelled(Exception):
    """Raised inside a job handler when the user cancelled the job."""


def now() -> datetime:
    return datetime.now(UTC)


def enqueue(
    db: Session,
    job_type: str,
    client_id: int | None = None,
    payload: dict[str, Any] | None = None,
    triggered_by: str = "user",
    created_by_id: int | None = None,
    dedupe: bool = True,
) -> Job:
    """Queue a job and return it. With ``dedupe``, an identical queued/running job is returned instead."""
    if job_type not in JOB_TYPES:
        raise ValueError(f"Unknown job type: {job_type}")
    if dedupe:
        existing = db.scalar(
            select(Job)
            .where(Job.type == job_type, Job.client_id.is_(client_id) if client_id is None else Job.client_id == client_id)
            .where(Job.status.in_(ACTIVE_STATUSES))
            .order_by(Job.id.desc())
            .limit(1)
        )
        if existing is not None:
            return existing
    job = Job(
        type=job_type,
        client_id=client_id,
        payload=payload or {},
        status="queued",
        progress=0,
        log="",
        cancel_requested=False,
        triggered_by=triggered_by,
        created_by_id=created_by_id,
    )
    db.add(job)
    db.commit()
    db.execute(text("SELECT pg_notify('jobs', :id)"), {"id": str(job.id)})
    db.commit()
    return job


def claim_next(db: Session) -> Job | None:
    """Atomically take the oldest queued job and mark it running."""
    row = db.execute(
        text(
            "UPDATE jobs SET status = 'running', started_at = now(), heartbeat_at = now() "
            "WHERE id = (SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at, id "
            "            FOR UPDATE SKIP LOCKED LIMIT 1) "
            "RETURNING id"
        )
    ).first()
    db.commit()
    if row is None:
        return None
    return db.get(Job, row[0], populate_existing=True)


def retry(db: Session, job: Job, user_id: int | None = None) -> Job:
    """Queue a fresh copy of a finished job."""
    return enqueue(db, job.type, job.client_id, dict(job.payload), triggered_by="user", created_by_id=user_id)


def request_cancel(db: Session, job: Job) -> None:
    """Cancel a queued job immediately, or ask a running job to stop at its next checkpoint."""
    if job.status == "queued":
        job.status = "cancelled"
        job.finished_at = now()
    elif job.status == "running":
        job.cancel_requested = True
    db.commit()


# Safe to run again from the start: unchanged pages and documents are skipped.
RESUMABLE = ("crawl", "index_docs", "scan_folder", "reindex")
MAX_ATTEMPTS = 3


def recover_stale_jobs(db: Session, stale_after: timedelta = timedelta(minutes=1)) -> int:
    """Handle running jobs whose worker stopped sending heartbeats (e.g. a container restart).

    Indexing-type jobs are queued again (up to MAX_ATTEMPTS runs); others are marked failed.
    """
    cutoff = now() - stale_after
    stale = list(db.scalars(select(Job).where(Job.status == "running", (Job.heartbeat_at < cutoff) | Job.heartbeat_at.is_(None))))
    for job in stale:
        attempts = int((job.payload or {}).get("_attempts", 1))
        if job.type in RESUMABLE and attempts < MAX_ATTEMPTS:
            job.status = "queued"
            job.payload = {**(job.payload or {}), "_attempts": attempts + 1}
            job.started_at = None
            job.heartbeat_at = None
            job.log = (job.log or "") + "The worker restarted during this job; running it again.\n"
        else:
            job.status = "failed"
            job.error = "The worker stopped while running this job. Use Retry to run it again."
            job.finished_at = now()
    db.commit()
    if any(j.status == "queued" for j in stale):
        db.execute(text("SELECT pg_notify('jobs', 'recovered')"))
        db.commit()
    return len(stale)


class JobContext:
    """Handed to job handlers: report progress, write log lines, check for cancellation.

    Uses its own short transactions so progress is visible immediately in the UI.
    """

    def __init__(self, db: Session, job: Job) -> None:
        self.db = db
        self.job = job
        self._last_flush = 0.0

    @property
    def payload(self) -> dict[str, Any]:
        return self.job.payload

    def log(self, message: str) -> None:
        stamp = now().strftime("%H:%M:%S")
        logger.info("[job %s] %s", self.job.id, message)
        self.db.execute(
            text("UPDATE jobs SET log = right(log || :line, :max), heartbeat_at = now() WHERE id = :id"),
            {"line": f"[{stamp}] {message}\n", "max": MAX_LOG_CHARS, "id": self.job.id},
        )
        self.db.commit()

    def progress(self, percent: float, text_: str | None = None) -> None:
        pct = max(0, min(100, int(percent)))
        self.db.execute(
            text("UPDATE jobs SET progress = :p, progress_text = coalesce(:t, progress_text), heartbeat_at = now() WHERE id = :id"),
            {"p": pct, "t": text_, "id": self.job.id},
        )
        self.db.commit()

    def check_cancelled(self) -> None:
        """Raise :class:`JobCancelled` if the user pressed Cancel."""
        cancelled = self.db.execute(text("SELECT cancel_requested FROM jobs WHERE id = :id"), {"id": self.job.id}).scalar()
        if cancelled:
            raise JobCancelled()

    def heartbeat(self) -> None:
        self.db.execute(text("UPDATE jobs SET heartbeat_at = now() WHERE id = :id"), {"id": self.job.id})
        self.db.commit()


def finish(db: Session, job: Job, status: str, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    db.execute(
        text(
            "UPDATE jobs SET status = :s, result = CAST(:r AS jsonb), error = :e, finished_at = now(), "
            "progress = CASE WHEN CAST(:done AS boolean) THEN 100 ELSE progress END WHERE id = :id"
        ),
        {"s": status, "r": _json(result), "e": error, "done": status == "succeeded", "id": job.id},
    )
    db.commit()


def _json(value: Any) -> str | None:
    import json

    return None if value is None else json.dumps(value, default=str)


def job_to_dict(job: Job, include_log: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": job.id,
        "type": job.type,
        "type_label": JOB_TYPES.get(job.type, job.type),
        "client_id": job.client.client_id if job.client else None,
        "client_name": job.client.name if job.client else None,
        "status": job.status,
        "progress": job.progress,
        "progress_text": job.progress_text,
        "result": job.result,
        "error": job.error,
        "cancel_requested": job.cancel_requested,
        "triggered_by": job.triggered_by,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }
    if include_log:
        data["log"] = job.log
        data["payload"] = job.payload
    return data
