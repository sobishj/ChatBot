"""Settings → AI Models: add/edit/delete models, test connections, default + fallback."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.admin.deps import require_setup_completed, require_super_admin
from app.db.models import AIModel, User
from app.db.session import get_db
from app.llm.providers import PROVIDERS
from app.services.ai_models import (
    ModelError,
    config_from_form,
    delete_model,
    list_models,
    model_to_dict,
    record_test,
    run_test,
    save_model,
)
from app.services.settings import get_settings_map, set_setting

router = APIRouter(
    prefix="/api/admin/models",
    tags=["models"],
    dependencies=[Depends(require_setup_completed), Depends(require_super_admin)],
)


def _get(db: Session, model_id: int) -> AIModel:
    model = db.get(AIModel, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found.")
    return model


def _listing(db: Session) -> dict[str, Any]:
    values = get_settings_map(db, ["default_model_id", "fallback_model_id", "mode"])
    return {
        "models": [model_to_dict(m, values["default_model_id"], values["fallback_model_id"]) for m in list_models(db)],
        "providers": [p.as_dict() for p in PROVIDERS.values()],
        "default_model_id": values["default_model_id"],
        "fallback_model_id": values["fallback_model_id"],
        "mode": values["mode"],
    }


@router.get("")
def list_all(db: Session = Depends(get_db)) -> dict[str, Any]:
    return _listing(db)


@router.post("")
def create(body: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        model = save_model(db, body)
    except (ModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"model": model_to_dict(model), **_listing(db)}


@router.put("/{model_id}")
def update(model_id: int, body: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    model = _get(db, model_id)
    try:
        save_model(db, body, model)
    except (ModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return _listing(db)


@router.delete("/{model_id}")
def remove(model_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    delete_model(db, _get(db, model_id))
    db.commit()
    return _listing(db)


class TestIn(BaseModel):
    model_id: int | None = None  # reuse the stored API key when testing an edited model
    config: dict[str, Any]


@router.post("/test")
def test_unsaved(body: TestIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Test the values currently in the form (before saving)."""
    existing = _get(db, body.model_id) if body.model_id else None
    try:
        cfg = config_from_form(body.config, existing)
    except (ModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return run_test(cfg)


@router.post("/{model_id}/test")
def test_saved(model_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.llm.client import ModelConfig

    model = _get(db, model_id)
    result = run_test(ModelConfig.from_row(model))
    record_test(db, model, result["ok"], result.get("reply") or result.get("error") or "")
    db.commit()
    return result


@router.post("/{model_id}/default")
def make_default(model_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    _get(db, model_id)
    set_setting(db, "default_model_id", model_id)
    db.commit()
    return _listing(db)


class FallbackIn(BaseModel):
    model_id: int | None


@router.post("/fallback")
def set_fallback(body: FallbackIn, _: User = Depends(require_super_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.model_id is not None:
        _get(db, body.model_id)
    set_setting(db, "fallback_model_id", body.model_id)
    db.commit()
    return _listing(db)
