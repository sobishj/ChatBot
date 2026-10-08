"""Login / logout / current user."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.admin.deps import client_ip, get_current_user, require_user
from app.db.models import User
from app.db.session import get_db
from app.security.sessions import clear_session, issue_session
from app.services import rate_limit
from app.services.settings import get_setting, get_settings_map
from app.services.users import authenticate, set_password, user_to_dict

router = APIRouter(prefix="/api/admin/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
    limit = int(get_setting(db, "rate_limits").get("login_per_ip_per_15min", 10))
    if not rate_limit.hit(db, f"login:{client_ip(request)}", limit, 15 * 60):
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again in 15 minutes.")
    user = authenticate(db, body.email, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    db.commit()
    issue_session(response, request, user.id, user.session_version)
    return {"user": user_to_dict(user)}


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    clear_session(response)
    return {"ok": True}


@router.get("/me")
def me(user: User | None = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Session bootstrap for the SPA: who am I, is setup done, which mode."""
    values = get_settings_map(db, ["setup_completed", "mode", "onprem_client_id"])
    onprem_slug = None
    if values["mode"] == "onprem" and values["onprem_client_id"]:
        from app.db.models import Client

        client = db.get(Client, values["onprem_client_id"])
        onprem_slug = client.client_id if client else None
    return {
        "user": user_to_dict(user) if user else None,
        "setup_completed": bool(values["setup_completed"]),
        "mode": values["mode"],
        "onprem_client_id": onprem_slug,
    }


@router.post("/password")
def change_password(
    body: PasswordIn, request: Request, response: Response, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> dict[str, bool]:
    from app.security.passwords import verify_password
    from app.services.users import UserError

    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    try:
        set_password(user, body.new_password)
    except UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    issue_session(response, request, user.id, user.session_version)  # keep this session valid
    return {"ok": True}
