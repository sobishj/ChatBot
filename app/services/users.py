"""Admin users: creation, authentication and serialisation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Client, User
from app.security.passwords import hash_password, needs_rehash, password_problem, verify_password

ROLES = ("super_admin", "client_admin")

# Verified against when the email is unknown, so response time doesn't reveal which emails exist.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


class UserError(ValueError):
    """Validation error with a message safe to show in the UI."""


def normalize_email(email: str) -> str:
    return email.strip().lower()


def get_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == normalize_email(email)))


def count_users(db: Session) -> int:
    return db.scalar(select(func.count(User.id))) or 0


def create_user(
    db: Session, name: str, email: str, password: str, role: str, client_ids: list[int] | None = None
) -> User:
    if role not in ROLES:
        raise UserError("Invalid role.")
    if not name.strip():
        raise UserError("Name is required.")
    if get_by_email(db, email):
        raise UserError("A user with this email already exists.")
    if problem := password_problem(password):
        raise UserError(problem)
    user = User(
        name=name.strip(),
        email=normalize_email(email),
        password_hash=hash_password(password),
        role=role,
        active=True,
        session_version=1,
    )
    if role == "client_admin" and client_ids:
        user.clients = list(db.scalars(select(Client).where(Client.id.in_(client_ids))))
    db.add(user)
    db.flush()
    return user


def set_password(user: User, password: str) -> None:
    if problem := password_problem(password):
        raise UserError(problem)
    user.password_hash = hash_password(password)
    user.session_version += 1  # log out other sessions


def authenticate(db: Session, email: str, password: str) -> User | None:
    """Return the active user for valid credentials, else None."""
    user = get_by_email(db, email)
    if user is None:
        verify_password(_DUMMY_HASH, password)
        return None
    if not verify_password(user.password_hash, password) or not user.active:
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = datetime.now(UTC)
    return user


def accessible_client_ids(user: User) -> list[int] | None:
    """Client ids the user may access; None means all (super admin)."""
    if user.role == "super_admin":
        return None
    return [c.id for c in user.clients]


def user_to_dict(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "active": user.active,
        "last_login_at": user.last_login_at,
        "created_at": user.created_at,
        "clients": [{"id": c.id, "client_id": c.client_id, "name": c.name} for c in user.clients],
    }
