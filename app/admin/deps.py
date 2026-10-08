"""FastAPI dependencies for the admin API: current user, roles, client access."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.models import Client, User
from app.db.session import get_db
from app.security.sessions import read_session
from app.services.clients import get_by_slug
from app.services.settings import get_setting


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """The logged-in, active user for this request, or None."""
    data = read_session(request)
    if data is None:
        return None
    user = db.get(User, data["u"])
    if user is None or not user.active or user.session_version != data["v"]:
        return None
    return user


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return user


def require_super_admin(user: User = Depends(require_user)) -> User:
    if user.role != "super_admin":
        raise HTTPException(status_code=403, detail="Only super admins can do this.")
    return user


def require_setup_completed(db: Session = Depends(get_db)) -> None:
    if not get_setting(db, "setup_completed"):
        raise HTTPException(status_code=409, detail="setup_required")


def can_access_client(user: User, client: Client) -> bool:
    return user.role == "super_admin" or any(c.id == client.id for c in user.clients)


def get_client_for_user(client_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)) -> Client:
    """Resolve ``{client_id}`` (slug) from the path, enforcing per-user access.

    Inaccessible and missing clients both return 404, so client_admins can't probe
    which other clients exist.
    """
    client = get_by_slug(db, client_id)
    if client is None or not can_access_client(user, client):
        raise HTTPException(status_code=404, detail="Client not found.")
    return client


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
