"""Read a client's OpenAPI (3.x) or Swagger (2.0) spec and turn its endpoints into action drafts.

The admin picks endpoints from the list; each becomes a normal action (validated like a
hand-made one), so nothing the admin didn't choose is ever callable by the assistant.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from app.actions.executor import ActionError, build_request, check_target, send
from app.db.models import Client, ClientAction

# Where APIs usually publish their spec (tried relative to the API URL, then to the host root).
CANDIDATE_PATHS = (
    "/openapi.json",
    "/swagger.json",
    "/v3/api-docs",
    "/swagger/v1/swagger.json",
    "/api-docs",
    "/openapi.yaml",
    "/swagger.yaml",
)
MAX_SPEC_BYTES = 5_000_000
METHODS = ("get", "post", "put", "patch", "delete")
_VERBS = {"GET": "get", "POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete"}
_NAME_OK = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_PARAM_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class SpecError(ValueError):
    """The spec can't be found or read; the message is safe to show in the UI."""


# ----------------------------------------------------------------------------- loading
def parse_text(text: str) -> dict[str, Any]:
    """JSON or YAML text → dict."""
    text = text.strip()
    if not text:
        raise SpecError("The spec is empty.")
    try:
        data = json.loads(text)
    except ValueError:
        import yaml

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SpecError("This is neither valid JSON nor YAML.") from exc
    if not isinstance(data, dict) or not ("openapi" in data or "swagger" in data) or not isinstance(data.get("paths"), dict):
        raise SpecError("This isn't an OpenAPI or Swagger spec (no 'openapi'/'swagger' version and 'paths').")
    return data


def fetch_spec(client: Client, spec_url: str | None, allow_private: bool) -> tuple[dict[str, Any], str]:
    """Download the spec (with the client's API authentication); returns (spec, url used).

    ``spec_url`` may be absolute or a path relative to the API URL. Without it, the
    usual locations are tried. Every URL passes the same SSRF check as action calls.
    """
    base = str((client.api_settings or {}).get("base_url") or "").rstrip("/")
    if spec_url:
        spec_url = spec_url.strip()
        candidates = [spec_url if urlparse(spec_url).scheme else base + "/" + spec_url.lstrip("/")]
    else:
        if not base:
            raise SpecError("Save the API URL first.")
        root = "{0.scheme}://{0.netloc}".format(urlparse(base))
        candidates = [base + p for p in CANDIDATE_PATHS] + ([root + p for p in CANDIDATE_PATHS] if root != base else [])
    errors: list[str] = []
    for url in dict.fromkeys(candidates):
        try:
            check_target(url, allow_private)
        except ActionError as exc:
            raise SpecError(str(exc)) from exc
        # The API key is only sent to the client's own API host, never to another site.
        same_host = bool(base) and urlparse(url).netloc == urlparse(base).netloc
        headers, query = _auth(client) if same_host else ({"Accept": "application/json"}, {})
        resp, error, _ms = send(client, "GET", url, headers, query, None, allow_private)
        if resp is None:
            errors.append(f"{url}: {error}")
            continue
        if resp.status_code != 200:
            errors.append(f"{url}: HTTP {resp.status_code}")
            continue
        if len(resp.content) > MAX_SPEC_BYTES:
            raise SpecError("The spec is larger than 5 MB.")
        try:
            return parse_text(resp.text), url
        except SpecError as exc:
            errors.append(f"{url}: {exc}")
    if spec_url:
        raise SpecError(f"Could not load the spec. {errors[0] if errors else ''}".strip())
    raise SpecError("No spec found at the usual addresses (/openapi.json, /swagger.json, /v3/api-docs …). Enter its URL or upload the file.")


def _auth(client: Client) -> tuple[dict[str, str], dict[str, Any]]:
    """The client's auth headers/query for fetching the spec from its own API (the key never leaves the server)."""
    probe = ClientAction(name="spec", method="GET", path="", parameters=[])
    try:
        _m, _u, headers, query, _b = build_request(client, probe, {})
    except ActionError:
        return {"Accept": "application/json"}, {}
    return headers, query


# ----------------------------------------------------------------------------- parsing
def _resolve(spec: dict[str, Any], node: Any, depth: int = 0) -> Any:
    """Follow local $refs (#/components/... or #/definitions/...)."""
    while isinstance(node, dict) and "$ref" in node and depth < 20:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return {}
        target: Any = spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part) if isinstance(target, dict) else None
            if target is None:
                return {}
        node = target
        depth += 1
    return node


def _unwrap(spec: dict[str, Any], schema: Any) -> dict[str, Any]:
    """Resolve refs and optional-wrappers (anyOf/oneOf with null, single allOf)."""
    schema = _resolve(spec, schema) or {}
    for key in ("anyOf", "oneOf"):
        options = [o for o in schema.get(key, []) if _resolve(spec, o).get("type") != "null"]
        if len(options) == 1 and key in schema:
            merged = {k: v for k, v in schema.items() if k != key}
            return {**_unwrap(spec, options[0]), **{k: v for k, v in merged.items() if k in ("description", "title")}}
    if len(schema.get("allOf", [])) == 1:
        return {**_unwrap(spec, schema["allOf"][0]), **{k: v for k, v in schema.items() if k != "allOf"}}
    return schema


def _param_type(name: str, schema: dict[str, Any]) -> str | None:
    """Our parameter type for a JSON schema, or None if it can't be a single value."""
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    fmt = str(schema.get("format") or "")
    lowered = name.lower()
    if kind in ("array", "object") or (kind is None and ("properties" in schema or "items" in schema)):
        return None
    if kind == "integer":
        return "integer"
    if kind == "number":
        return "number"
    if kind == "boolean":
        return "boolean"
    if fmt in ("date", "date-time"):
        return "date"
    if fmt == "email" or lowered.endswith("email"):
        return "email"
    if any(word in lowered for word in ("phone", "mobile", "msisdn")):
        return "phone"
    return "string"


def _snake(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def suggest_name(method: str, path: str, operation_id: str | None) -> str:
    """operationId when it reads well; otherwise verb + path words (get_slots, delete_appointments_by_id)."""
    if operation_id:
        name = _snake(operation_id)
        # FastAPI-style generated ids (book_appointments_post) read badly: derive a name instead.
        if _NAME_OK.match(name) and not name.endswith("_" + method.lower()):
            return name[:63]
    words = []
    for segment in path.strip("/").split("/"):
        if not segment:
            continue
        placeholder = re.fullmatch(r"\{(.+)\}", segment)
        words.append("by_" + _snake(placeholder.group(1)) if placeholder else _snake(segment))
    name = "_".join([_VERBS[method]] + [w for w in words if w]) or _VERBS[method]
    if not name[0].isalpha():
        name = "op_" + name
    return name[:63].rstrip("_")


def endpoints(spec: dict[str, Any], auth_name: str = "X-API-Key", base_url: str = "") -> list[dict[str, Any]]:
    """Every operation as an action draft plus notes about what couldn't be imported."""
    prefix = _server_prefix(spec, base_url)
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for raw_path, item in (spec.get("paths") or {}).items():
        item = _resolve(spec, item)
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            out.append(_operation(spec, method.upper(), prefix + raw_path, op, shared, auth_name, used))
    return out


def _server_prefix(spec: dict[str, Any], base_url: str) -> str:
    """Path prefix from servers[0] (3.x) or basePath (2.0) that the API URL doesn't already include."""
    if "swagger" in spec:
        server_path = str(spec.get("basePath") or "")
    else:
        servers = spec.get("servers") or []
        url = str(servers[0].get("url") or "") if servers and isinstance(servers[0], dict) else ""
        server_path = urlparse(urljoin("http://x/", url)).path if url else ""
    server_path = server_path.rstrip("/")
    if not server_path or server_path == "/":
        return ""
    return "" if urlparse(base_url).path.rstrip("/").endswith(server_path) else server_path


def _operation(spec: dict[str, Any], method: str, path: str, op: dict[str, Any], shared: list[Any], auth_name: str, used: set[str]) -> dict[str, Any]:
    notes: list[str] = []
    params: list[dict[str, Any]] = []
    importable = True
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for p in [*shared, *(op.get("parameters") or [])]:  # operation-level parameters override path-level ones
        p = _resolve(spec, p)
        if isinstance(p, dict) and p.get("name"):
            merged[(p["name"], p.get("in", ""))] = p

    for (name, where), p in merged.items():
        required = bool(p.get("required")) or where == "path"
        if where in ("header", "cookie"):
            if name.lower() in (auth_name.lower(), "authorization"):
                continue  # the connection's authentication covers it
            notes.append(f"{where} parameter {name} is not supported")
            importable = importable and not required
            continue
        if where == "body":  # Swagger 2.0 JSON body
            importable = _body_fields(spec, p.get("schema"), bool(p.get("required")), params, notes) and importable
            continue
        location = {"query": "query", "path": "path", "formData": "body"}.get(where)
        schema = _unwrap(spec, p.get("schema") or {k: v for k, v in p.items() if k in ("type", "format", "enum", "items")})
        kind = _param_type(name, schema)
        if location is None or kind is None or not _PARAM_OK.match(name):
            notes.append(f"parameter {name} ({where}) can't be imported")
            importable = importable and not required
            continue
        params.append(_param(name, kind, location, required, p.get("description") or schema.get("description") or schema.get("title"), schema))

    body = op.get("requestBody")
    if body:
        body = _resolve(spec, body)
        content = body.get("content") or {}
        media = content.get("application/json") or next((v for k, v in content.items() if "json" in k), None)
        if media is None:
            notes.append("request body is not JSON")
            importable = importable and not body.get("required")
        else:
            importable = _body_fields(spec, media.get("schema"), bool(body.get("required")), params, notes) and importable

    summary = str(op.get("summary") or "").strip()
    detail = str(op.get("description") or "").strip()
    description = ". ".join(x.rstrip(".") for x in (summary, detail) if x)
    if len(description) < 40:  # e.g. FastAPI's "Book": give the model the endpoint too
        description = f"{description} ({method} {path})" if description else f"{method} {path}"
    name = suggest_name(method, path, op.get("operationId"))
    base, n = name, 2
    while name in used:
        name = f"{base[:60]}_{n}"
        n += 1
    used.add(name)
    if op.get("deprecated"):
        notes.append("deprecated in the spec")
    placeholders = set(re.findall(r"\{([^}]+)\}", path))
    if placeholders - {p["name"] for p in params if p["location"] == "path"}:
        notes.append("path has placeholders without a usable parameter")
        importable = False
    return {
        "key": f"{method} {path}",
        "method": method,
        "path": path,
        "summary": summary,
        "importable": importable,
        "notes": notes,
        "action": {
            "name": name,
            "description": description[:1000],
            "method": method,
            "path": path,
            "parameters": params,
            "requires_confirmation": method != "GET",
            "enabled": True,
            "response_hint": "",
        },
    }


def _body_fields(spec: dict[str, Any], schema: Any, required_body: bool, params: list[dict[str, Any]], notes: list[str]) -> bool:
    """Top-level fields of a JSON object body become body parameters. Returns False if a required field can't."""
    schema = _unwrap(spec, schema)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        notes.append("request body is not a plain JSON object")
        return not required_body
    required = set(schema.get("required") or [])
    ok = True
    for name, prop in properties.items():
        prop = _unwrap(spec, prop)
        if prop.get("readOnly"):
            continue
        kind = _param_type(name, prop)
        if kind is None or not _PARAM_OK.match(name):
            notes.append(f"body field {name} (list or nested object) can't be imported")
            ok = ok and name not in required
            continue
        params.append(_param(name, kind, "body", name in required, prop.get("description") or prop.get("title"), prop))
    return ok


def _param(name: str, kind: str, location: str, required: bool, description: Any, schema: dict[str, Any]) -> dict[str, Any]:
    param: dict[str, Any] = {
        "name": name,
        "type": kind,
        "location": location,
        "required": required,
        "description": str(description or "").strip()[:300],
    }
    enum = schema.get("enum")
    if isinstance(enum, list) and enum and all(isinstance(e, str | int | float) and not isinstance(e, bool) for e in enum):
        param["enum"] = [str(e) for e in enum][:50]
    return param
