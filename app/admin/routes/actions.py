"""Client → API actions: connection settings, action definitions, tests and the call log.

client_admins may view (the key is always masked); every change and every live call
needs a super admin. Disabling keeps the configuration but stops its use at once.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.actions.executor import ActionError, allow_private_targets, build_request, execute, send
from app.admin.deps import get_client_for_user, require_setup_completed, require_super_admin, require_user
from app.db.models import Client, ClientAction, User
from app.db.session import get_db
from app.services.actions import (
    ActionConfigError,
    action_to_dict,
    add_template,
    list_actions,
    list_calls,
    save_action,
    settings_view,
    update_settings,
)
from app.services.settings import get_setting

router = APIRouter(prefix="/api/admin/clients/{client_id}/api-actions", tags=["api-actions"], dependencies=[Depends(require_setup_completed)])


def _view(db: Session, client: Client, user: User) -> dict[str, Any]:
    mode = get_setting(db, "mode")
    return {
        "enabled": client.api_actions_enabled,
        "settings": settings_view(client),
        "actions": [action_to_dict(a) for a in list_actions(db, client)],
        "can_edit": user.role == "super_admin",
        "mode": mode,
        "allows_private": allow_private_targets(mode),
    }


def _action(db: Session, client: Client, action_id: int) -> ClientAction:
    action = db.get(ClientAction, action_id)
    if action is None or action.client_id != client.id:
        raise HTTPException(status_code=404, detail="Action not found.")
    return action


@router.get("")
def get_api_actions(client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _view(db, client, user)


@router.put("/settings")
def put_settings(
    body: dict[str, Any], client: Client = Depends(get_client_for_user), user: User = Depends(require_super_admin), db: Session = Depends(get_db)
) -> dict[str, Any]:
    try:
        update_settings(client, body, allow_private_targets(get_setting(db, "mode")))
    except ActionConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return _view(db, client, user)


@router.post("/actions")
def create_action(
    body: dict[str, Any], client: Client = Depends(get_client_for_user), user: User = Depends(require_super_admin), db: Session = Depends(get_db)
) -> dict[str, Any]:
    try:
        save_action(db, client, body)
    except ActionConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return _view(db, client, user)


@router.post("/actions/template")
def apply_template(client: Client = Depends(get_client_for_user), user: User = Depends(require_super_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Add the appointment-booking actions (only those that don't exist yet)."""
    added = add_template(db, client)
    db.commit()
    return {**_view(db, client, user), "added": added}


@router.put("/actions/{action_id}")
def update_action(
    action_id: int, body: dict[str, Any], client: Client = Depends(get_client_for_user), user: User = Depends(require_super_admin), db: Session = Depends(get_db)
) -> dict[str, Any]:
    action = _action(db, client, action_id)
    try:
        save_action(db, client, body, action)
    except ActionConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return _view(db, client, user)


@router.delete("/actions/{action_id}")
def delete_action(
    action_id: int, client: Client = Depends(get_client_for_user), user: User = Depends(require_super_admin), db: Session = Depends(get_db)
) -> dict[str, Any]:
    db.delete(_action(db, client, action_id))
    db.commit()
    return _view(db, client, user)


class ActionTestIn(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    # Required for anything but GET: the call goes to the client's live API and may change data.
    live: bool = False


@router.post("/actions/{action_id}/test")
def test_action(
    action_id: int, body: ActionTestIn, client: Client = Depends(get_client_for_user), user: User = Depends(require_super_admin), db: Session = Depends(get_db)
) -> dict[str, Any]:
    action = _action(db, client, action_id)
    if action.method != "GET" and not body.live:
        raise HTTPException(status_code=400, detail="This action changes data in the client's live system. Tick “I understand this will call the live API” to run it.")
    if not (client.api_settings or {}).get("base_url"):
        raise HTTPException(status_code=400, detail="Set the API URL first.")
    result = execute(
        db, client, action, body.params, session_id=f"admin-test-{user.id}", channel="admin",
        allow_private=allow_private_targets(get_setting(db, "mode")),
    )
    db.commit()
    return result.as_dict()


@router.post("/test-connection")
def test_connection(client: Client = Depends(get_client_for_user), _: User = Depends(require_super_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    """GET the health path (or the API URL itself) with the configured authentication."""
    settings = client.api_settings or {}
    if not settings.get("base_url"):
        raise HTTPException(status_code=400, detail="Set the API URL first.")
    probe = ClientAction(name="test_connection", method="GET", path=settings.get("health_path") or "", parameters=[])
    try:
        method, url, headers, query, _body = build_request(client, probe, {})
    except ActionError as exc:
        return {"ok": False, "error": str(exc)}
    resp, error, ms = send(client, method, url, headers, query, None, allow_private_targets(get_setting(db, "mode")))
    if resp is None:
        return {"ok": False, "error": error, "response_ms": ms}
    ok = resp.status_code < 400
    return {
        "ok": ok,
        "status_code": resp.status_code,
        "response_ms": ms,
        "excerpt": (resp.text or "")[:300],
        "error": None if ok else f"HTTP {resp.status_code}",
    }


@router.get("/calls")
def get_calls(
    page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=100), client: Client = Depends(get_client_for_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return list_calls(db, client, page, size)
