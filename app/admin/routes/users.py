"""Users & roles (super admin only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.admin.deps import require_setup_completed, require_super_admin
from app.db.models import Client, User
from app.db.session import get_db
from app.services.users import ROLES, UserError, create_user, get_by_email, normalize_email, set_password, user_to_dict

router = APIRouter(
    prefix="/api/admin/users", tags=["users"], dependencies=[Depends(require_setup_completed), Depends(require_super_admin)]
)


def _listing(db: Session) -> dict[str, Any]:
    users = db.scalars(select(User).order_by(User.name))
    clients = db.scalars(select(Client).order_by(Client.name))
    return {
        "users": [user_to_dict(u) for u in users],
        "clients": [{"id": c.id, "client_id": c.client_id, "name": c.name} for c in clients],
        "roles": list(ROLES),
    }


def _active_super_admins(db: Session) -> int:
    return db.scalar(select(func.count(User.id)).where(User.role == "super_admin", User.active.is_(True))) or 0


@router.get("")
def list_users(db: Session = Depends(get_db)) -> dict[str, Any]:
    return _listing(db)


class UserIn(BaseModel):
    name: str
    email: str
    password: str
    role: str
    client_ids: list[int] = []


@router.post("")
def add_user(body: UserIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    if "@" not in body.email:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    create_user(db, body.name, body.email, body.password, body.role, body.client_ids)
    db.commit()
    return _listing(db)


class UserPatch(BaseModel):
    name: str | None = None
    email: str | None = None
    role: str | None = None
    active: bool | None = None
    password: str | None = None
    client_ids: list[int] | None = None


@router.patch("/{user_id}")
def update_user(user_id: int, body: UserPatch, me: User = Depends(require_super_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    demoting = body.role is not None and body.role != "super_admin" and user.role == "super_admin"
    disabling = body.active is False and user.active and user.role == "super_admin"
    if (demoting or disabling) and _active_super_admins(db) <= 1:
        raise HTTPException(status_code=400, detail="There must always be at least one active super admin.")
    if user.id == me.id and body.active is False:
        raise HTTPException(status_code=400, detail="You cannot disable your own account.")
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="Name is required.")
        user.name = body.name.strip()
    if body.email is not None and normalize_email(body.email) != user.email:
        if "@" not in body.email or get_by_email(db, body.email):
            raise HTTPException(status_code=400, detail="Invalid email, or it is already in use.")
        user.email = normalize_email(body.email)
    if body.role is not None:
        if body.role not in ROLES:
            raise UserError("Invalid role.")
        user.role = body.role
    if body.active is not None:
        user.active = body.active
        if not body.active:
            user.session_version += 1  # sign the user out everywhere
    if body.password:
        set_password(user, body.password)
    if body.client_ids is not None:
        user.clients = list(db.scalars(select(Client).where(Client.id.in_(body.client_ids)))) if body.client_ids else []
    if user.role == "super_admin":
        user.clients = []
    db.commit()
    return _listing(db)
