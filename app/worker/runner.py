"""Job runner: worker threads claim queued jobs and dispatch them to handlers."""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable
from typing import Any

from sqlalchemy import text

from app.db.session import new_session, session_scope
from app.embeddings.model import current_device, gpu_info
from app.services import jobs as job_service
from app.services.jobs import JobCancelled, JobContext
from app.services.settings import set_setting

logger = logging.getLogger(__name__)

Handler = Callable[[JobContext], dict[str, Any] | None]
HANDLERS: dict[str, Handler] = {}

POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 10.0


def handler(job_type: str) -> Callable[[Handler], Handler]:
    """Decorator registering a job handler."""

    def register(fn: Handler) -> Handler:
        HANDLERS[job_type] = fn
        return fn

    return register


def run_job(job_id: int) -> str:
    """Execute one already-claimed job; returns the final status."""
    db = new_session()
    try:
        job = db.get(job_service.Job, job_id)
        if job is None:
            return "missing"
        fn = HANDLERS.get(job.type)
        if fn is None:
            job_service.finish(db, job, "failed", error=f"No handler for job type '{job.type}'.")
            return "failed"
        ctx = JobContext(db, job)
        try:
            ctx.log(f"Started: {job_service.JOB_TYPES.get(job.type, job.type)}")
            result = fn(ctx) or {}
        except JobCancelled:
            db.rollback()
            ctx.log("Cancelled by user")
            job_service.finish(db, job, "cancelled")
            return "cancelled"
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job
            db.rollback()
            logger.exception("Job %s failed", job_id)
            ctx.log("ERROR: " + "".join(traceback.format_exception_only(type(exc), exc)).strip())
            job_service.finish(db, job, "failed", error=str(exc)[:2000] or exc.__class__.__name__)
            return "failed"
        ctx.log("Finished")
        job_service.finish(db, job, "succeeded", result=result)
        return "succeeded"
    finally:
        db.close()


class Worker:
    """Runs ``threads`` job loops plus a heartbeat loop until :meth:`stop` is called."""

    def __init__(self, threads: int = 2) -> None:
        self.threads = threads
        self._stop = threading.Event()
        self._wake = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                with session_scope() as db:
                    job = job_service.claim_next(db)
                    job_id = job.id if job else None
                if job_id is not None:
                    run_job(job_id)
                    continue
            except Exception:  # noqa: BLE001 - keep the loop alive (e.g. DB restart)
                logger.exception("Worker loop error")
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def _heartbeat(self) -> None:
        while not self._stop.is_set():
            try:
                with session_scope() as db:
                    now_ = db.execute(text("SELECT now()")).scalar().isoformat()
                    # The GPU status is shown in Settings: the worker is where indexing runs.
                    set_setting(db, "worker_heartbeat", {"at": now_, "gpu": gpu_info(), "device": current_device()})
                    # Keep running jobs alive for the stale-job detector.
                    db.execute(text("UPDATE jobs SET heartbeat_at = now() WHERE status = 'running'"))
            except Exception:  # noqa: BLE001
                logger.warning("Heartbeat failed", exc_info=True)
            self._stop.wait(HEARTBEAT_SECONDS)

    def run(self) -> None:
        with session_scope() as db:
            recovered = job_service.recover_stale_jobs(db)
            if recovered:
                logger.warning("Marked %s interrupted job(s) as failed", recovered)
        threads = [threading.Thread(target=self._loop, name=f"job-{i}", daemon=True) for i in range(self.threads)]
        threads.append(threading.Thread(target=self._heartbeat, name="heartbeat", daemon=True))
        for t in threads:
            t.start()
        logger.info("Worker started with %s job thread(s)", self.threads)
        self._stop.wait()
        for t in threads:
            t.join(timeout=10)
