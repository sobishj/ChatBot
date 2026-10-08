"""First-run setup wizard API (/setup in the UI). Disabled once setup is completed.

Step 1 (create the super admin) is open to anyone, but only while no user exists.
Every later step requires that super admin to be signed in.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.admin.deps import get_current_user, require_super_admin
from app.db.models import AIModel, Client, Job, User
from app.db.session import get_db
from app.llm.providers import PROVIDERS
from app.security.sessions import issue_session
from app.services import jobs as job_service
from app.services.ai_models import ModelError, config_from_form, model_to_dict, record_test, run_test, save_model
from app.services.clients import ClientError, client_to_dict, create_client
from app.services.settings import get_setting, get_settings_map, set_setting, update_setting_dict
from app.services.users import UserError, count_users, create_user, user_to_dict

router = APIRouter(prefix="/api/admin/setup", tags=["setup"])

DOMAIN_RE = re.compile(r"^[a-z0-9.-]+(:\d{1,5})?$")


def _ensure_not_completed(db: Session) -> None:
    if get_setting(db, "setup_completed"):
        raise HTTPException(status_code=409, detail="Setup is already completed.")


def _advance(db: Session, step: int) -> None:
    if int(get_setting(db, "setup_step") or 1) < step:
        set_setting(db, "setup_step", step)


def normalize_public_domain(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"^https?://", "", value).split("/")[0]
    if not value or not DOMAIN_RE.match(value):
        raise HTTPException(status_code=400, detail="Enter a domain like chat.example.com (no http:// or path).")
    return value


# ----------------------------------------------------------------------------- state
@router.get("/state")
def state(user: User | None = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    values = get_settings_map(
        db, ["setup_completed", "setup_step", "mode", "public_domain", "embedding", "default_model_id", "onprem_client_id"]
    )
    model = db.get(AIModel, values["default_model_id"]) if values["default_model_id"] else None
    client = db.get(Client, values["onprem_client_id"]) if values["onprem_client_id"] else None
    embedding = values["embedding"] or {}
    job = db.get(Job, embedding["job_id"]) if embedding.get("job_id") else None
    return {
        "completed": bool(values["setup_completed"]),
        "step": int(values["setup_step"] or 1),
        "has_admin": count_users(db) > 0,
        "user": user_to_dict(user) if user else None,
        "mode": values["mode"],
        "public_domain": values["public_domain"],
        "model": model_to_dict(model, values["default_model_id"]) if model else None,
        "embedding": {k: embedding.get(k) for k in ("model", "dim", "status", "error")},
        "embedding_job": job_service.job_to_dict(job) if job else None,
        "client": client_to_dict(client) if client else None,
        "providers": [p.as_dict() for p in PROVIDERS.values()],
    }


# ----------------------------------------------------------------------------- step 1
class AdminIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=500)


@router.post("/admin")
def create_admin(body: AdminIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
    _ensure_not_completed(db)
    if count_users(db) > 0:
        raise HTTPException(status_code=409, detail="The admin account already exists. Please sign in.")
    if "@" not in body.email:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    try:
        user = create_user(db, body.name, body.email, body.password, "super_admin")
    except UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _advance(db, 2)
    db.commit()
    issue_session(response, request, user.id, user.session_version)
    return {"user": user_to_dict(user)}


# ----------------------------------------------------------------------------- steps 2-3
class ModeIn(BaseModel):
    mode: str


@router.post("/mode")
def choose_mode(body: ModeIn, _: User = Depends(require_super_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    _ensure_not_completed(db)
    if body.mode not in ("cloud", "onprem"):
        raise HTTPException(status_code=400, detail="Choose cloud or onprem.")
    set_setting(db, "mode", body.mode)
    _advance(db, 3)
    db.commit()
    return {"mode": body.mode}


class DomainIn(BaseModel):
    public_domain: str


@router.post("/domain")
def set_domain(body: DomainIn, _: User = Depends(require_super_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    _ensure_not_completed(db)
    domain = normalize_public_domain(body.public_domain)
    set_setting(db, "public_domain", domain)
    _advance(db, 4)
    db.commit()
    return {"public_domain": domain}


# ----------------------------------------------------------------------------- step 4
@router.post("/model/test")
def test_model(body: dict[str, Any], _: User = Depends(require_super_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    _ensure_not_completed(db)
    try:
        cfg = config_from_form(body)
    except (ModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return run_test(cfg)


@router.post("/model")
def save_first_model(body: dict[str, Any], _: User = Depends(require_super_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Save the first model, but only after a successful live connection test."""
    _ensure_not_completed(db)
    existing_id = get_setting(db, "default_model_id")
    existing = db.get(AIModel, existing_id) if existing_id else None
    try:
        cfg = config_from_form(body, existing)
    except (ModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = run_test(cfg)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=f"Connection test failed: {result['error']}")
    try:
        model = save_model(db, body, existing)
    except (ModelError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    set_setting(db, "default_model_id", model.id)
    record_test(db, model, True, result.get("reply", ""))
    _advance(db, 5)
    db.commit()
    return {"model": model_to_dict(model, model.id), "test": result}


# ----------------------------------------------------------------------------- step 5
class EmbeddingIn(BaseModel):
    model: str = Field(min_length=3, max_length=200)


@router.post("/embedding")
def start_embedding_download(
    body: EmbeddingIn, user: User = Depends(require_super_admin), db: Session = Depends(get_db)
) -> dict[str, Any]:
    _ensure_not_completed(db)
    return start_embedding_job(db, body.model.strip(), user.id)


def start_embedding_job(db: Session, model_name: str, user_id: int | None) -> dict[str, Any]:
    """Queue the download job (shared with Settings → Embeddings)."""
    if not re.match(r"^[\w.-]+/[\w.-]+$", model_name):
        raise HTTPException(status_code=400, detail="Enter a Hugging Face model id like BAAI/bge-m3.")
    current = get_setting(db, "embedding") or {}
    if current.get("status") == "ready" and current.get("model") == model_name:
        return {"embedding": current, "job": None}
    previous = current.get("model") if current.get("status") == "ready" else None
    job = job_service.enqueue(
        db, "download_embedding", payload={"model": model_name, "previous_model": previous}, created_by_id=user_id
    )
    merged = update_setting_dict(db, "embedding", {"status": "downloading", "error": None, "job_id": job.id, "pending_model": model_name})
    db.commit()
    return {"embedding": merged, "job": job_service.job_to_dict(job)}


@router.post("/embedding/continue")
def embedding_continue(_: User = Depends(require_super_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    _ensure_not_completed(db)
    embedding = get_setting(db, "embedding") or {}
    if embedding.get("status") not in ("ready", "downloading"):
        raise HTTPException(status_code=400, detail="Start the embedding model download first.")
    next_step = 6 if get_setting(db, "mode") == "onprem" else 7
    _advance(db, next_step)
    db.commit()
    return {"step": next_step}


# ----------------------------------------------------------------------------- step 6
class ClientIn(BaseModel):
    client_id: str
    name: str
    website_url: str = ""
    allowed_domains: list[str] = []


@router.post("/client")
def create_onprem_client(body: ClientIn, _: User = Depends(require_super_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    _ensure_not_completed(db)
    if get_setting(db, "mode") != "onprem":
        raise HTTPException(status_code=400, detail="This step is only for on-premise installs.")
    existing_id = get_setting(db, "onprem_client_id")
    if existing_id and db.get(Client, existing_id):
        raise HTTPException(status_code=409, detail="The client was already created.")
    try:
        client = create_client(db, body.client_id, body.name, body.website_url, body.allowed_domains)
    except ClientError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    set_setting(db, "onprem_client_id", client.id)
    _advance(db, 7)
    db.commit()
    return {"client": client_to_dict(client)}


# ----------------------------------------------------------------------------- step 7
@router.post("/finish")
def finish(_: User = Depends(require_super_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    _ensure_not_completed(db)
    values = get_settings_map(db, ["default_model_id", "embedding", "mode", "onprem_client_id"])
    if not values["default_model_id"]:
        raise HTTPException(status_code=400, detail="Add an AI model first.")
    if (values["embedding"] or {}).get("status") not in ("ready", "downloading"):
        raise HTTPException(status_code=400, detail="Start the embedding model download first.")
    if values["mode"] == "onprem" and not values["onprem_client_id"]:
        raise HTTPException(status_code=400, detail="Create the client first.")
    set_setting(db, "setup_completed", True)
    db.commit()
    return {"completed": True}
