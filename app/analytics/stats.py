"""Analytics over the ``questions`` table: stats, conversations, CSV export, retention."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Client, Question

VISITOR_CHANNELS = ("widget",)  # Test chat and CLI questions are excluded from statistics


def _scope(stmt: Select[Any], client_ids: list[int] | None, since: datetime, include_test: bool) -> Select[Any]:
    stmt = stmt.where(Question.created_at >= since)
    if client_ids is not None:
        stmt = stmt.where(Question.client_id.in_(client_ids or [-1]))
    if not include_test:
        stmt = stmt.where(Question.channel.in_(VISITOR_CHANNELS))
    return stmt


def question_stats(db: Session, client_ids: list[int] | None, days: int = 30, include_test: bool = False) -> dict[str, Any]:
    """Everything the Stats tab shows, for one or more clients."""
    now = datetime.now(UTC)
    since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    totals = db.execute(
        _scope(
            select(
                func.count(Question.id),
                func.count(Question.id).filter(Question.answered.is_(False)),
                func.coalesce(func.sum(Question.input_tokens), 0),
                func.coalesce(func.sum(Question.output_tokens), 0),
                func.coalesce(func.sum(Question.cost), 0.0),
                func.coalesce(func.avg(Question.response_ms), 0),
            ),
            client_ids,
            since,
            include_test,
        )
    ).one()
    total, unanswered, tokens_in, tokens_out, cost, avg_ms = totals

    day = func.date_trunc("day", Question.created_at).label("day")
    per_day_rows = db.execute(
        _scope(
            select(day, func.count(Question.id), func.count(Question.id).filter(Question.answered.is_(False))),
            client_ids,
            since,
            include_test,
        ).group_by(day)
    ).all()
    by_day = {r[0].date(): (r[1], r[2]) for r in per_day_rows}
    per_day = []
    for i in range(days):
        d: date = (since + timedelta(days=i)).date()
        t, u = by_day.get(d, (0, 0))
        per_day.append({"date": d.isoformat(), "total": t, "unanswered": u})

    top_rows = db.execute(
        _scope(
            select(
                Question.question_normalized,
                func.count(Question.id).label("n"),
                func.min(Question.question),
                func.count(Question.id).filter(Question.answered.is_(False)),
            ),
            client_ids,
            since,
            include_test,
        )
        .where(Question.question_normalized != "")
        .group_by(Question.question_normalized)
        .order_by(func.count(Question.id).desc())
        .limit(20)
    ).all()

    unanswered_rows = db.execute(
        _scope(
            select(Question.id, Question.question, Question.created_at, Question.top_score, Question.client_id),
            client_ids,
            since,
            include_test,
        )
        .where(Question.answered.is_(False))
        .order_by(Question.created_at.desc())
        .limit(50)
    ).all()

    lang_rows = db.execute(
        _scope(select(Question.language, func.count(Question.id)), client_ids, since, include_test)
        .group_by(Question.language)
        .order_by(func.count(Question.id).desc())
    ).all()

    model_rows = db.execute(
        _scope(
            select(
                Question.model_name,
                func.count(Question.id),
                func.coalesce(func.sum(Question.input_tokens), 0),
                func.coalesce(func.sum(Question.output_tokens), 0),
                func.coalesce(func.sum(Question.cost), 0.0),
                func.count(Question.id).filter(Question.used_fallback.is_(True)),
            ),
            client_ids,
            since,
            include_test,
        ).group_by(Question.model_name)
    ).all()

    return {
        "days": days,
        "total": total,
        "answered": total - unanswered,
        "unanswered": unanswered,
        "unanswered_rate": (unanswered / total) if total else 0.0,
        "avg_response_ms": int(avg_ms or 0),
        "tokens": {"input": int(tokens_in), "output": int(tokens_out), "cost": round(float(cost), 6)},
        "per_day": per_day,
        "top_questions": [
            {"question": r[2], "normalized": r[0], "count": r[1], "unanswered": r[3]} for r in top_rows
        ],
        "unanswered_questions": [
            {"id": r[0], "question": r[1], "created_at": r[2], "top_score": r[3]} for r in unanswered_rows
        ],
        "languages": [{"language": r[0] or "unknown", "count": r[1]} for r in lang_rows],
        "models": [
            {"model": r[0] or "—", "count": r[1], "input_tokens": int(r[2]), "output_tokens": int(r[3]), "cost": round(float(r[4]), 6), "fallbacks": r[5]}
            for r in model_rows
        ],
    }


def questions_today(db: Session, client_ids: list[int] | None) -> int:
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(_scope(select(func.count(Question.id)), client_ids, start, False)) or 0


# ----------------------------------------------------------------------------- conversations
def conversations_query(
    client_id: int, search: str = "", answered: str = "all", channel: str = "all", days: int | None = None
) -> Select[Any]:
    stmt = select(Question).where(Question.client_id == client_id)
    if search.strip():
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(Question.question.ilike(like), Question.answer.ilike(like)))
    if answered == "yes":
        stmt = stmt.where(Question.answered.is_(True))
    elif answered == "no":
        stmt = stmt.where(Question.answered.is_(False))
    if channel != "all":
        stmt = stmt.where(Question.channel == channel)
    if days:
        stmt = stmt.where(Question.created_at >= datetime.now(UTC) - timedelta(days=days))
    return stmt


def list_conversations(db: Session, client_id: int, page: int = 1, per_page: int = 50, **filters: Any) -> dict[str, Any]:
    base = conversations_query(client_id, **filters)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(base.order_by(Question.created_at.desc(), Question.id.desc()).offset((page - 1) * per_page).limit(per_page))
    return {"total": total, "page": page, "per_page": per_page, "items": [question_to_dict(q) for q in rows]}


def question_to_dict(q: Question) -> dict[str, Any]:
    return {
        "id": q.id,
        "session_id": q.session_id,
        "channel": q.channel,
        "question": q.question,
        "answer": q.answer,
        "sources": q.sources,
        "answered": q.answered,
        "top_score": q.top_score,
        "language": q.language,
        "model_name": q.model_name,
        "used_fallback": q.used_fallback,
        "input_tokens": q.input_tokens,
        "output_tokens": q.output_tokens,
        "cost": q.cost,
        "response_ms": q.response_ms,
        "error": q.error,
        "created_at": q.created_at,
    }


CSV_COLUMNS = ["created_at", "session_id", "channel", "language", "question", "answer", "answered", "top_score", "model_name", "used_fallback", "input_tokens", "output_tokens", "cost", "response_ms", "sources"]


def _csv_safe(value: Any) -> Any:
    """Prevent CSV/formula injection when the file is opened in Excel."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def export_csv(db: Session, client_id: int, **filters: Any) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    buffer.write("﻿")  # BOM so Excel opens UTF-8 (e.g. Malayalam) correctly
    writer.writerow(CSV_COLUMNS)
    yield buffer.getvalue()
    stmt = conversations_query(client_id, **filters).order_by(Question.created_at.desc()).execution_options(yield_per=500)
    for q in db.scalars(stmt):
        buffer.seek(0)
        buffer.truncate()
        data = question_to_dict(q)
        data["sources"] = "; ".join(s.get("url") or s.get("title", "") for s in (q.sources or []))
        writer.writerow([_csv_safe(data[c]) for c in CSV_COLUMNS])
        yield buffer.getvalue()


# ----------------------------------------------------------------------------- retention
def apply_retention(db: Session, retention_days: int) -> int:
    """Delete questions older than ``retention_days`` (0 = keep forever). Returns rows deleted."""
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    result = db.execute(delete(Question).where(Question.created_at < cutoff))
    db.commit()
    return result.rowcount or 0


def clients_with_activity(db: Session, client_ids: list[int] | None, days: int = 30) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=days)
    stmt = (
        select(Client.client_id, Client.name, func.count(Question.id))
        .join(Question, and_(Question.client_id == Client.id, Question.created_at >= since, Question.channel.in_(VISITOR_CHANNELS)), isouter=True)
        .group_by(Client.id)
        .order_by(func.count(Question.id).desc())
    )
    if client_ids is not None:
        stmt = stmt.where(Client.id.in_(client_ids or [-1]))
    return [{"client_id": r[0], "name": r[1], "questions": r[2]} for r in db.execute(stmt).all()]

