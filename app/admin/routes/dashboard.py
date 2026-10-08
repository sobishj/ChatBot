"""Dashboard summary (scoped to the clients the user can access)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.admin.deps import require_setup_completed, require_user
from app.admin.routes.system import health_summary
from app.analytics.stats import clients_with_activity, question_stats, questions_today
from app.db.models import Client, Job, User
from app.db.session import get_db
from app.services import jobs as job_service
from app.services.users import accessible_client_ids

router = APIRouter(prefix="/api/admin/dashboard", tags=["dashboard"], dependencies=[Depends(require_setup_completed)])


@router.get("")
def dashboard(user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    allowed = accessible_client_ids(user)
    client_stmt = select(func.count(Client.id))
    job_stmt = select(Job).order_by(Job.id.desc()).limit(8)
    if allowed is not None:
        client_stmt = client_stmt.where(Client.id.in_(allowed or [-1]))
        job_stmt = job_stmt.where(Job.client_id.in_(allowed or [-1]))
    stats = question_stats(db, allowed, days=30)
    return {
        "clients": db.scalar(client_stmt) or 0,
        "active_clients": db.scalar(client_stmt.where(Client.active.is_(True))) or 0,
        "questions_today": questions_today(db, allowed),
        "questions_30d": stats["total"],
        "unanswered_rate_30d": stats["unanswered_rate"],
        "tokens_30d": stats["tokens"],
        "per_day": stats["per_day"],
        "top_clients": clients_with_activity(db, allowed)[:5],
        "recent_jobs": [job_service.job_to_dict(j) for j in db.scalars(job_stmt)],
        "health": health_summary(db) if user.role == "super_admin" else None,
    }
