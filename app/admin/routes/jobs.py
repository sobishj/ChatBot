"""Jobs page: list, detail with logs, retry, cancel."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin.deps import require_setup_completed, require_user
from app.db.models import Client, Job, User
from app.db.session import get_db
from app.services import jobs as job_service
from app.services.users import accessible_client_ids

router = APIRouter(prefix="/api/admin/jobs", tags=["jobs"])


def _visible(user: User, job: Job) -> bool:
    allowed = accessible_client_ids(user)
    if allowed is None:
        return True
    return job.client_id is not None and job.client_id in allowed


def _get(db: Session, user: User, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None or not _visible(user, job):
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@router.get("", dependencies=[Depends(require_setup_completed)])
def list_jobs(
    status: str | None = None,
    client: str | None = None,
    job_type: str | None = Query(None, alias="type"),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(Job).order_by(Job.id.desc()).limit(limit)
    allowed = accessible_client_ids(user)
    if allowed is not None:
        stmt = stmt.where(Job.client_id.in_(allowed or [-1]))
    if status:
        stmt = stmt.where(Job.status == status)
    if job_type:
        stmt = stmt.where(Job.type == job_type)
    if client:
        stmt = stmt.join(Client, Client.id == Job.client_id).where(Client.client_id == client)
    jobs = list(db.scalars(stmt))
    return {"jobs": [job_service.job_to_dict(j) for j in jobs], "types": job_service.JOB_TYPES}


@router.get("/{job_id}")
def get_job(job_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Also used by the setup wizard (embedding download progress), so no setup check here."""
    return {"job": job_service.job_to_dict(_get(db, user, job_id), include_log=True)}


@router.post("/{job_id}/retry", dependencies=[Depends(require_setup_completed)])
def retry(job_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    job = _get(db, user, job_id)
    if job.status in job_service.ACTIVE_STATUSES:
        raise HTTPException(status_code=400, detail="The job is still running.")
    new_job = job_service.retry(db, job, user.id)
    return {"job": job_service.job_to_dict(new_job)}


@router.post("/{job_id}/cancel", dependencies=[Depends(require_setup_completed)])
def cancel(job_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    job = _get(db, user, job_id)
    job_service.request_cancel(db, job)
    db.refresh(job)
    return {"job": job_service.job_to_dict(job)}
