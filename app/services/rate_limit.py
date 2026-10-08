"""Fixed-window rate limiting stored in Postgres (shared across processes and restarts)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from app.db.models import RateLimitCounter


def hit(db: Session, key: str, limit: int, window_seconds: int) -> bool:
    """Count one request for ``key``; return True if it is allowed, False if over ``limit``.

    A limit of 0 or less disables the check. Commits immediately so the counter is
    shared even if the caller's transaction later fails.
    """
    if limit <= 0:
        return True
    now = datetime.now(UTC)
    window_start = datetime.fromtimestamp(int(now.timestamp()) // window_seconds * window_seconds, UTC)
    count = db.execute(
        text(
            "INSERT INTO rate_limits (key, window_start, count) VALUES (:key, :ws, 1) "
            "ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limits.count + 1 "
            "RETURNING count"
        ),
        {"key": key[:200], "ws": window_start},
    ).scalar_one()
    db.commit()
    return count <= limit


def cleanup(db: Session, older_than: timedelta = timedelta(days=1)) -> int:
    """Delete old counters; returns the number removed."""
    cutoff = datetime.now(UTC) - older_than
    result = db.execute(delete(RateLimitCounter).where(RateLimitCounter.window_start < cutoff))
    db.commit()
    return result.rowcount or 0
