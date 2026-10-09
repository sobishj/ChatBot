"""Turn ``client_actions`` rows into OpenAI-style tool definitions (JSON Schema) for LiteLLM."""

from __future__ import annotations

from typing import Any

from app.db.models import ClientAction

_JSON_TYPES = {
    "string": {"type": "string"},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
    "phone": {"type": "string"},
    "email": {"type": "string", "format": "email"},
}
_TYPE_HINTS = {
    "date": "ISO date, YYYY-MM-DD.",
    "phone": "Phone number. Pass the visitor's placeholder (e.g. [phone_1]) unchanged.",
    "email": "Email address. Pass the visitor's placeholder (e.g. [email_1]) unchanged.",
}


def tool_definition(action: ClientAction) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in action.parameters or []:
        kind = p.get("type", "string")
        prop = dict(_JSON_TYPES.get(kind, {"type": "string"}))
        description = " ".join(x for x in (p.get("description") or "", _TYPE_HINTS.get(kind, "")) if x).strip()
        if description:
            prop["description"] = description
        if p.get("enum"):
            prop["enum"] = list(p["enum"])
        properties[p["name"]] = prop
        if p.get("required"):
            required.append(p["name"])
    description = action.description or action.name.replace("_", " ")
    if action.response_hint:
        description += f" Response: {action.response_hint}"
    if action.requires_confirmation:
        description += " The visitor is asked to confirm before this runs."
    return {
        "type": "function",
        "function": {
            "name": action.name,
            "description": description[:1000],
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        },
    }


def tools_for(actions: list[ClientAction]) -> list[dict[str, Any]]:
    return [tool_definition(a) for a in actions if a.enabled]
