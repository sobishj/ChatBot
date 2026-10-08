"""Settings (super admin): prompt, threshold, schedule, rate limits, domain, privacy, embeddings."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.admin.deps import require_setup_completed, require_super_admin
from app.admin.routes.setup import normalize_public_domain, start_embedding_job
from app.db.models import Job, User
from app.db.session import get_db
from app.services import jobs as job_service
from app.services.settings import DEFAULT_SYSTEM_PROMPT, DEFAULTS, get_settings_map, set_setting

router = APIRouter(
    prefix="/api/admin/settings",
    tags=["settings"],
    dependencies=[Depends(require_setup_completed), Depends(require_super_admin)],
)

EDITABLE = [
    "system_prompt",
    "confidence_threshold",
    "crawl_schedule",
    "timezone",
    "rate_limits",
    "public_domain",
    "chat_notice",
    "retention_days",
]
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _view(db: Session) -> dict[str, Any]:
    values = get_settings_map(db, [*EDITABLE, "mode", "embedding"])
    embedding = values.pop("embedding") or {}
    job = db.get(Job, embedding["job_id"]) if embedding.get("job_id") else None
    values["embedding"] = {k: embedding.get(k) for k in ("model", "dim", "status", "error", "pending_model")}
    values["embedding_job"] = job_service.job_to_dict(job) if job else None
    values["default_system_prompt"] = DEFAULT_SYSTEM_PROMPT
    return values


def _validate(key: str, value: Any) -> Any:
    """Validate one setting; raise HTTPException(400) with a readable message."""

    def bad(msg: str) -> HTTPException:
        return HTTPException(status_code=400, detail=msg)

    if key == "system_prompt":
        value = str(value or "").strip()
        if len(value) < 20:
            raise bad("The system prompt is too short.")
        return value[:20000]
    if key == "confidence_threshold":
        v = float(value)
        if not 0 <= v <= 1:
            raise bad("Confidence threshold must be between 0 and 1.")
        return v
    if key == "crawl_schedule":
        freq = value.get("frequency")
        if freq not in ("daily", "weekly", "off"):
            raise bad("Crawl frequency must be daily, weekly or off.")
        time_ = str(value.get("time", "03:00"))
        if not TIME_RE.match(time_):
            raise bad("Time must be HH:MM (24-hour).")
        weekday = int(value.get("weekday", 0))
        if not 0 <= weekday <= 6:
            raise bad("Invalid weekday.")
        return {"frequency": freq, "time": time_, "weekday": weekday}
    if key == "timezone":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(str(value))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise bad("Unknown time zone. Use a name like Asia/Kolkata or UTC.") from exc
        return str(value)
    if key == "rate_limits":
        out = dict(DEFAULTS["rate_limits"])
        for k in out:
            if k in value:
                n = int(value[k])
                if n < 0 or n > 100000:
                    raise bad("Rate limits must be between 0 (off) and 100000.")
                out[k] = n
        return out
    if key == "public_domain":
        return normalize_public_domain(str(value))
    if key == "chat_notice":
        return str(value or "").strip()[:500]
    if key == "retention_days":
        v = int(value)
        if v < 0 or v > 3650:
            raise bad("Retention must be between 0 (keep forever) and 3650 days.")
        return v
    raise bad(f"Unknown setting: {key}")


@router.get("")
def get_all(db: Session = Depends(get_db)) -> dict[str, Any]:
    return _view(db)


@router.put("")
def update(body: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    for key, value in body.items():
        if key not in EDITABLE:
            raise HTTPException(status_code=400, detail=f"Setting '{key}' cannot be changed here.")
        try:
            clean = _validate(key, value)
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid value for {key}.") from exc
        set_setting(db, key, clean)
    db.commit()
    return _view(db)


@router.post("/system-prompt/reset")
def reset_prompt(db: Session = Depends(get_db)) -> dict[str, Any]:
    set_setting(db, "system_prompt", DEFAULT_SYSTEM_PROMPT)
    db.commit()
    return _view(db)


class EmbeddingIn(BaseModel):
    model: str


@router.post("/embedding")
def change_embedding(body: EmbeddingIn, user: User = Depends(require_super_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Download (and switch to) another embedding model. Content is re-indexed automatically if the size changes."""
    start_embedding_job(db, body.model.strip(), user.id)
    return _view(db)
