"""Background schedule (runs inside the worker container).

A single "tick" every minute reads the current settings from the database, so
changes made in the admin UI apply without restarting anything:

* automatic re-crawl + document re-index of all active clients (daily/weekly/off)
* watched-folder scans per client (every N minutes)
* daily data retention cleanup and rate-limit counter cleanup
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.analytics.stats import apply_retention
from app.db.models import Client
from app.db.session import session_scope
from app.services import jobs as job_service
from app.services import rate_limit
from app.services.clients import document_settings
from app.services.settings import get_settings_map, set_setting

logger = logging.getLogger(__name__)


def crawl_due(schedule: dict[str, Any], now_local: datetime, last_run: str | None) -> bool:
    """True when the configured daily/weekly time has passed and we haven't run for that slot yet."""
    frequency = schedule.get("frequency", "daily")
    if frequency == "off":
        return False
    hour, minute = (int(x) for x in str(schedule.get("time", "03:00")).split(":"))
    slot = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if frequency == "weekly":
        slot -= timedelta(days=(now_local.weekday() - int(schedule.get("weekday", 0))) % 7)
    if now_local < slot:
        slot -= timedelta(days=7 if frequency == "weekly" else 1)
    if last_run is None:
        # First run of the scheduler: only fire if we're within an hour after the slot.
        return now_local - slot < timedelta(hours=1)
    return datetime.fromisoformat(last_run) < slot


def tick() -> None:
    try:
        with session_scope() as db:
            set_setting(db, "scheduler_heartbeat", {"at": datetime.now(UTC).isoformat()})
            values = get_settings_map(db, ["setup_completed", "crawl_schedule", "scheduler_state", "retention_days", "timezone"])
            if not values["setup_completed"]:
                return
            state: dict[str, Any] = values.get("scheduler_state") or {}
            tz = ZoneInfo(values.get("timezone") or "UTC")
            now_local = datetime.now(tz)

            # 1. Scheduled re-crawl + document re-index.
            if crawl_due(values["crawl_schedule"] or {}, now_local, state.get("last_crawl_run")):
                clients = list(db.scalars(select(Client).where(Client.active.is_(True))))
                for client in clients:
                    if client.website_url:
                        job_service.enqueue(db, "crawl", client.id, triggered_by="schedule")
                    job_service.enqueue(db, "index_docs", client.id, triggered_by="schedule")
                state["last_crawl_run"] = now_local.isoformat()
                logger.info("Scheduled re-crawl queued for %s client(s)", len(clients))

            # 2. Watched folders.
            for client in db.scalars(select(Client).where(Client.active.is_(True))):
                ds = document_settings(client)
                if not ds.get("watch_path"):
                    continue
                interval = max(5, int(ds.get("scan_interval_minutes") or 60))
                last = ds.get("last_scan_at")
                if last is None or datetime.fromisoformat(last) < datetime.now(UTC) - timedelta(minutes=interval):
                    job_service.enqueue(db, "scan_folder", client.id, triggered_by="schedule")

            # 3. Daily cleanups.
            today = datetime.now(UTC).date().isoformat()
            if state.get("last_cleanup") != today:
                deleted = apply_retention(db, int(values["retention_days"] or 0))
                removed = rate_limit.cleanup(db)
                state["last_cleanup"] = today
                logger.info("Retention cleanup: %s question(s) deleted, %s rate-limit counters removed", deleted, removed)

            set_setting(db, "scheduler_state", state)
    except Exception:  # noqa: BLE001 - never let the scheduler die
        logger.exception("Scheduler tick failed")


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1})
    scheduler.add_job(tick, "interval", minutes=1, id="tick", next_run_time=datetime.now(UTC) + timedelta(seconds=10))
    scheduler.start()
    logger.info("Scheduler started")
    return scheduler

