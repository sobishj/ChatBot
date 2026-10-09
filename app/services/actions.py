"""API actions admin: connection settings, action definitions, templates and the audit log view."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.actions.executor import (
    AUTH_TYPES,
    DEFAULT_TIMEOUT,
    LOCATIONS,
    METHODS,
    TYPES,
    ActionError,
    check_target,
)
from app.db.models import ActionCall, Client, ClientAction
from app.security.crypto import decrypt_secret, encrypt_secret, mask_secret

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
MAX_ACTIONS = 50
MAX_PARAMETERS = 30
MAX_EXTRA_HEADERS = 20


class ActionConfigError(ValueError):
    """Invalid API settings or action definition; the message is safe to show in the UI."""


# ----------------------------------------------------------------------------- connection settings
def settings_view(client: Client) -> dict[str, Any]:
    """API settings for the admin UI: the key is masked, never returned."""
    s = client.api_settings or {}
    key = decrypt_secret(s["api_key_encrypted"]) if s.get("api_key_encrypted") else None
    return {
        "base_url": s.get("base_url") or "",
        "auth_type": s.get("auth_type") or "header",
        "auth_name": s.get("auth_name") or "X-API-Key",
        "api_key_masked": mask_secret(key) if key else ("••••" if s.get("api_key_encrypted") else ""),
        "has_api_key": bool(s.get("api_key_encrypted")),
        "api_key_unreadable": bool(s.get("api_key_encrypted")) and key is None,
        "timeout_seconds": int(s.get("timeout_seconds") or DEFAULT_TIMEOUT),
        "extra_headers": dict(s.get("extra_headers") or {}),
        "health_path": s.get("health_path") or "",
    }


def update_settings(client: Client, data: dict[str, Any], allow_private: bool) -> None:
    """Validate and store connection settings. An empty ``api_key`` keeps the stored key."""
    current = dict(client.api_settings or {})
    new = dict(current)
    if "base_url" in data:
        base_url = str(data.get("base_url") or "").strip().rstrip("/")
        if base_url:
            if len(base_url) > 500:
                raise ActionConfigError("The API URL is too long.")
            try:
                check_target(base_url, allow_private)
            except ActionError as exc:
                raise ActionConfigError(str(exc)) from exc
        new["base_url"] = base_url
    if "auth_type" in data:
        if data["auth_type"] not in AUTH_TYPES:
            raise ActionConfigError("Authentication must be header, bearer or query.")
        new["auth_type"] = data["auth_type"]
    if "auth_name" in data:
        name = str(data.get("auth_name") or "").strip() or "X-API-Key"
        if not HEADER_RE.match(name):
            raise ActionConfigError("The header or parameter name may contain only letters, digits and - _ .")
        new["auth_name"] = name
    key = str(data.get("api_key") or "").strip()
    if key:
        if len(key) > 2000:
            raise ActionConfigError("The API key is too long.")
        new["api_key_encrypted"] = encrypt_secret(key)
    elif data.get("clear_api_key"):
        new.pop("api_key_encrypted", None)
    if "timeout_seconds" in data:
        try:
            timeout = int(data["timeout_seconds"])
        except (TypeError, ValueError) as exc:
            raise ActionConfigError("Timeout must be a number of seconds.") from exc
        if not 1 <= timeout <= 60:
            raise ActionConfigError("Timeout must be between 1 and 60 seconds.")
        new["timeout_seconds"] = timeout
    if "extra_headers" in data:
        headers = data.get("extra_headers") or {}
        if not isinstance(headers, dict) or len(headers) > MAX_EXTRA_HEADERS:
            raise ActionConfigError(f"Extra headers must be at most {MAX_EXTRA_HEADERS} name/value pairs.")
        clean: dict[str, str] = {}
        for name, value in headers.items():
            name, value = str(name).strip(), str(value)
            if not name:
                continue
            if not HEADER_RE.match(name) or name.lower() in ("host", "content-length", "transfer-encoding", "connection"):
                raise ActionConfigError(f"Invalid header name: {name}")
            if len(value) > 500 or "\r" in value or "\n" in value:
                raise ActionConfigError(f"Invalid value for header {name}.")
            clean[name] = value
        new["extra_headers"] = clean
    if "health_path" in data:
        path = str(data.get("health_path") or "").strip()
        if path and (not path.startswith("/") or "://" in path or len(path) > 300):
            raise ActionConfigError("The health check path must start with / (e.g. /health).")
        new["health_path"] = path
    client.api_settings = new
    if "enabled" in data:
        client.api_actions_enabled = bool(data["enabled"])


# ----------------------------------------------------------------------------- actions
def validate_action(db: Session, client: Client, data: dict[str, Any], existing: ClientAction | None = None) -> dict[str, Any]:
    """Return clean fields for a ClientAction, or raise ActionConfigError."""
    name = str(data.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ActionConfigError("Name must be snake_case: lower-case letters, digits and _ (e.g. get_slots).")
    clash = db.scalar(select(ClientAction.id).where(ClientAction.client_id == client.id, ClientAction.name == name))
    if clash is not None and (existing is None or clash != existing.id):
        raise ActionConfigError(f"An action called {name} already exists.")
    method = str(data.get("method") or "GET").upper()
    if method not in METHODS:
        raise ActionConfigError(f"Method must be one of {', '.join(METHODS)}.")
    path = str(data.get("path") or "").strip()
    if not path.startswith("/") or "://" in path or ".." in path or len(path) > 500 or any(c.isspace() for c in path):
        raise ActionConfigError("Path must start with / and be relative to the API URL, e.g. /doctors/{doctor_id}/slots.")
    description = str(data.get("description") or "").strip()
    if not description:
        raise ActionConfigError("Describe when the assistant should use this action.")

    raw_params = data.get("parameters") or []
    if not isinstance(raw_params, list) or len(raw_params) > MAX_PARAMETERS:
        raise ActionConfigError(f"An action can have at most {MAX_PARAMETERS} parameters.")
    params: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in raw_params:
        if not isinstance(p, dict):
            raise ActionConfigError("Invalid parameter.")
        pname = str(p.get("name") or "").strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", pname):
            raise ActionConfigError(f"Invalid parameter name: {pname or '(empty)'}")
        if pname in seen:
            raise ActionConfigError(f"Parameter {pname} appears twice.")
        seen.add(pname)
        kind = p.get("type") or "string"
        location = p.get("location") or "query"
        if kind not in TYPES:
            raise ActionConfigError(f"Parameter {pname}: type must be one of {', '.join(TYPES)}.")
        if location not in LOCATIONS:
            raise ActionConfigError(f"Parameter {pname}: location must be path, query or body.")
        if location == "body" and method == "GET":
            raise ActionConfigError(f"Parameter {pname}: GET requests have no body; use query.")
        enum = p.get("enum") or []
        if not isinstance(enum, list) or len(enum) > 50:
            raise ActionConfigError(f"Parameter {pname}: allowed values must be a list (at most 50).")
        clean = {
            "name": pname,
            "type": kind,
            "location": location,
            "required": bool(p.get("required")) or location == "path",
            "description": str(p.get("description") or "").strip()[:300],
        }
        if enum:
            clean["enum"] = [str(e).strip()[:100] for e in enum if str(e).strip()]
        params.append(clean)
    path_params = {p["name"] for p in params if p["location"] == "path"}
    placeholders = set(PLACEHOLDER_RE.findall(path))
    if placeholders != path_params:
        missing = placeholders - path_params
        extra = path_params - placeholders
        if missing:
            detail = f"declare {', '.join(sorted(missing))} as path parameter(s)"
        else:
            detail = "add " + ", ".join("{" + name + "}" for name in sorted(extra)) + " to the path"
        raise ActionConfigError(f"Path placeholders and path parameters don't match: {detail}.")
    return {
        "name": name,
        "description": description[:1000],
        "method": method,
        "path": path,
        "parameters": params,
        # Changing data always needs the visitor's confirmation; only GET may skip it.
        "requires_confirmation": True if method != "GET" else bool(data.get("requires_confirmation", False)),
        "enabled": bool(data.get("enabled", True)),
        "response_hint": (str(data.get("response_hint") or "").strip()[:1000]) or None,
    }


def save_action(db: Session, client: Client, data: dict[str, Any], existing: ClientAction | None = None) -> ClientAction:
    fields = validate_action(db, client, data, existing)
    if existing is None:
        count = db.scalar(select(func.count()).select_from(ClientAction).where(ClientAction.client_id == client.id)) or 0
        if count >= MAX_ACTIONS:
            raise ActionConfigError(f"A client can have at most {MAX_ACTIONS} actions.")
    action = existing or ClientAction(client_id=client.id)
    for key, value in fields.items():
        setattr(action, key, value)
    if existing is None:
        db.add(action)
    db.flush()
    return action


def action_to_dict(action: ClientAction) -> dict[str, Any]:
    return {
        "id": action.id,
        "name": action.name,
        "description": action.description,
        "method": action.method,
        "path": action.path,
        "parameters": action.parameters or [],
        "requires_confirmation": action.requires_confirmation,
        "enabled": action.enabled,
        "response_hint": action.response_hint or "",
        "updated_at": action.updated_at,
    }


def list_actions(db: Session, client: Client) -> list[ClientAction]:
    return list(db.scalars(select(ClientAction).where(ClientAction.client_id == client.id).order_by(ClientAction.name)))


# ----------------------------------------------------------------------------- template
def _p(name: str, kind: str, location: str, description: str, required: bool = True) -> dict[str, Any]:
    return {"name": name, "type": kind, "location": location, "required": required, "description": description}


# Matches demo/mock_booking_api.py; admins adjust paths and parameters to the client's real API.
APPOINTMENT_TEMPLATE: list[dict[str, Any]] = [
    {
        "name": "list_departments", "method": "GET", "path": "/departments", "parameters": [],
        "description": "List the hospital's departments (e.g. Cardiology). Use when the visitor wants to book or asks which departments exist.",
        "response_hint": "Each department has an id and a name.",
    },
    {
        "name": "list_doctors", "method": "GET", "path": "/doctors",
        "parameters": [_p("department_id", "integer", "query", "Only doctors of this department.", required=False)],
        "description": "List doctors, optionally for one department. Use to find the doctor the visitor wants.",
        "response_hint": "Each doctor has an id, name and department_id.",
    },
    {
        "name": "get_slots", "method": "GET", "path": "/slots",
        "parameters": [_p("doctor_id", "integer", "query", "The doctor's id from list_doctors."), _p("date", "date", "query", "The day to check.")],
        "description": "Get a doctor's free appointment times on a date. Always check before offering a time.",
        "response_hint": "A list of free times like 09:30.",
    },
    {
        "name": "book_appointment", "method": "POST", "path": "/appointments",
        "parameters": [
            _p("doctor_id", "integer", "body", "The doctor's id."),
            _p("date", "date", "body", "Appointment date."),
            _p("time", "string", "body", "A free time returned by get_slots, e.g. 09:30."),
            _p("patient_name", "string", "body", "The patient's full name."),
            _p("phone", "phone", "body", "The patient's phone number."),
        ],
        "description": "Book an appointment once the visitor has chosen a doctor, date and free time and given name and phone.",
        "response_hint": "Returns booking_id; tell the visitor the booking ID.",
    },
    {
        "name": "cancel_appointment", "method": "DELETE", "path": "/appointments/{appointment_id}",
        "parameters": [_p("appointment_id", "string", "path", "The booking ID to cancel.")],
        "description": "Cancel an existing appointment by its booking ID.",
        "response_hint": "Confirms the cancellation.",
    },
]


def add_template(db: Session, client: Client) -> list[str]:
    """Create the appointment-booking actions that don't exist yet; returns the names added."""
    existing = {a.name for a in list_actions(db, client)}
    added = []
    for template in APPOINTMENT_TEMPLATE:
        if template["name"] in existing:
            continue
        save_action(db, client, {**template, "enabled": True})
        added.append(template["name"])
    return added


# ----------------------------------------------------------------------------- audit log
def list_calls(db: Session, client: Client, page: int = 1, size: int = 25) -> dict[str, Any]:
    size = max(1, min(size, 100))
    page = max(1, page)
    total = db.scalar(select(func.count()).select_from(ActionCall).where(ActionCall.client_id == client.id)) or 0
    rows = db.scalars(
        select(ActionCall).where(ActionCall.client_id == client.id).order_by(ActionCall.created_at.desc(), ActionCall.id.desc())
        .offset((page - 1) * size).limit(size)
    )
    return {
        "total": total,
        "page": page,
        "size": size,
        "calls": [
            {
                "id": c.id, "action": c.action_name, "channel": c.channel, "ok": c.ok, "status_code": c.status_code,
                "error": c.error, "response_ms": c.response_ms, "params": (c.request_summary or {}).get("params", {}),
                "method": (c.request_summary or {}).get("method"), "path": (c.request_summary or {}).get("path"),
                "question_id": c.question_id, "created_at": c.created_at,
            }
            for c in rows
        ],
    }
