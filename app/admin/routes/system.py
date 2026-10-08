"""System health, AI model checks and database backup (super admin)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import __version__
from app.admin.deps import require_setup_completed, require_super_admin
from app.config import get_settings
from app.db.models import AIModel
from app.db.session import check_database, get_db
from app.llm.client import ModelConfig
from app.services.ai_models import list_models, model_to_dict, record_test, run_test
from app.services.settings import get_settings_map

router = APIRouter(
    prefix="/api/admin/system", tags=["system"], dependencies=[Depends(require_setup_completed), Depends(require_super_admin)]
)

HEARTBEAT_STALE_SECONDS = 90


def _age_seconds(value: dict[str, Any] | None) -> float | None:
    if not value or not value.get("at"):
        return None
    return (datetime.now(UTC) - datetime.fromisoformat(value["at"])).total_seconds()


def health_summary(db: Session) -> dict[str, Any]:
    """Compact status used by the dashboard and the System page."""
    values = get_settings_map(db, ["worker_heartbeat", "scheduler_heartbeat", "embedding", "default_model_id"])
    worker_age = _age_seconds(values.get("worker_heartbeat"))
    scheduler_age = _age_seconds(values.get("scheduler_heartbeat"))
    embedding = values.get("embedding") or {}
    models = list_models(db)
    return {
        "database": check_database()["ok"],
        "worker": worker_age is not None and worker_age < HEARTBEAT_STALE_SECONDS,
        "worker_age_seconds": worker_age,
        "scheduler": scheduler_age is not None and scheduler_age < HEARTBEAT_STALE_SECONDS,
        "scheduler_age_seconds": scheduler_age,
        "embedding": embedding.get("status"),
        "embedding_model": embedding.get("model"),
        "models_total": len(models),
        "models_failing": sum(1 for m in models if m.last_test_ok is False),
        "default_model_set": bool(values.get("default_model_id")),
    }


def _disk(path: str) -> dict[str, Any] | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {"path": path, "total": usage.total, "used": usage.used, "free": usage.free}


@router.get("")
def system_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    db_info: dict[str, Any] = {}
    try:
        db_info = {
            "version": db.execute(text("SHOW server_version")).scalar(),
            "size_bytes": db.execute(text("SELECT pg_database_size(current_database())")).scalar(),
            "pgvector": db.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar(),
        }
    except Exception:  # noqa: BLE001
        db_info = {}
    values = get_settings_map(db, ["default_model_id", "fallback_model_id", "embedding", "mode"])
    return {
        "version": __version__,
        "mode": values["mode"],
        "summary": health_summary(db),
        "database": db_info,
        "embedding": {k: (values["embedding"] or {}).get(k) for k in ("model", "dim", "status", "error")},
        "models": [model_to_dict(m, values["default_model_id"], values["fallback_model_id"]) for m in list_models(db)],
        "disk": [d for d in (_disk(str(settings.data_dir)), _disk(str(settings.watched_dir))) if d],
        "time": datetime.now(UTC),
    }


@router.post("/models/{model_id}/check")
def check_model(model_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    model = db.get(AIModel, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found.")
    result = run_test(ModelConfig.from_row(model))
    record_test(db, model, result["ok"], result.get("reply") or result.get("error") or "")
    db.commit()
    return result


@router.get("/backup")
def backup() -> StreamingResponse:
    """Stream a pg_dump (custom format) of the whole database.

    The dump includes encrypted API keys: they can only be decrypted with the same SECRET_KEY.
    Uploaded files live in the Docker volume and are not part of this dump.
    """
    url = make_url(get_settings().database_url)
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    cmd = [
        "pg_dump", "--format=custom", "--no-owner",
        "-h", url.host or "db", "-p", str(url.port or 5432), "-U", url.username or "postgres", url.database or "postgres",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)  # noqa: S603 - fixed argv
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="pg_dump is not installed in this container.") from exc

    def stream() -> Iterator[bytes]:
        assert proc.stdout is not None
        try:
            while chunk := proc.stdout.read(1024 * 256):
                yield chunk
        finally:
            proc.stdout.close()
            proc.wait(timeout=30)

    filename = f"website-assistant-{time.strftime('%Y%m%d-%H%M%S')}.dump"
    return StreamingResponse(stream(), media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
