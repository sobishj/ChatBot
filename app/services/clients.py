"""Client (tenant) management: validation, defaults, allowed origins, public config."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Chunk, Client, Document, Page, Question

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULT_BRANDING: dict[str, Any] = {
    "bot_name": "Assistant",
    "primary_color": "#1f5f5b",
    "greeting": "Hi! How can I help you today?",
    "default_language": "en",
    "position": "right",  # right | left
    "powered_by": True,
    "powered_by_text": "Powered by Website Assistant",
    "powered_by_url": "",
    "logo_file": None,  # filename inside the client's branding folder
}

DEFAULT_CRAWL_SETTINGS: dict[str, Any] = {
    "max_pages": 500,
    "delay_seconds": 1.0,
    "include_patterns": [],  # e.g. ["/stores/*"]; empty = everything
    "exclude_patterns": [],  # e.g. ["/careers/*", "*?print=*"]
}

DEFAULT_DOCUMENT_SETTINGS: dict[str, Any] = {
    "watch_path": None,  # absolute path under WATCHED_DIR, or None
    "scan_interval_minutes": 60,
    "last_scan_at": None,
}


class ClientError(ValueError):
    """Validation error with a message safe to show in the UI."""


# ----------------------------------------------------------------------------- validation
def validate_slug(slug: str) -> str:
    slug = slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise ClientError("Client ID may contain lowercase letters, digits and hyphens (max 64 characters).")
    return slug


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ClientError("Enter a valid website URL, e.g. https://www.example.com")
    return url


def normalize_domain(value: str) -> str:
    """Turn user input ('https://www.Example.com/path', '*.example.com', 'localhost:5500') into a pattern."""
    value = value.strip().lower()
    if not value:
        raise ClientError("Empty domain.")
    wildcard = value.startswith("*.")
    if wildcard:
        value = value[2:]
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    host = parsed.hostname
    if not host or not re.match(r"^[a-z0-9.-]+$", host):
        raise ClientError(f"Invalid domain: {value}")
    result = host + (f":{parsed.port}" if parsed.port else "")
    return ("*." + result) if wildcard else result


def normalize_domains(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        if v and v.strip():
            d = normalize_domain(v)
            if d not in out:
                out.append(d)
    return out


def origin_allowed(origin: str | None, allowed_domains: list[str]) -> bool:
    """Check a browser ``Origin`` header against a client's allowed domains.

    ``example.com`` also allows ``www.example.com`` (and vice versa); ``*.example.com``
    allows any subdomain. A pattern with a port must match the port exactly.
    """
    if not origin or origin == "null":
        return False
    parsed = urlparse(origin)
    host = (parsed.hostname or "").lower()
    if not host or parsed.scheme not in ("http", "https"):
        return False
    port = parsed.port
    for pattern in allowed_domains:
        pattern = pattern.lower()
        p_port: int | None = None
        if ":" in pattern:
            pattern, _, port_s = pattern.rpartition(":")
            p_port = int(port_s) if port_s.isdigit() else None
            if p_port != port:
                continue
        if pattern.startswith("*."):
            base = pattern[2:]
            if host == base or host.endswith("." + base):
                return True
        elif host == pattern or host.removeprefix("www.") == pattern.removeprefix("www."):
            return True
    return False


# ----------------------------------------------------------------------------- CRUD
def get_by_slug(db: Session, slug: str) -> Client | None:
    return db.scalar(select(Client).where(Client.client_id == slug))


def create_client(db: Session, slug: str, name: str, website_url: str, allowed_domains: list[str] | None = None) -> Client:
    slug = validate_slug(slug)
    if get_by_slug(db, slug):
        raise ClientError("A client with this ID already exists.")
    if not name.strip():
        raise ClientError("Name is required.")
    website_url = normalize_url(website_url)
    domains = normalize_domains(allowed_domains or [])
    if not domains and website_url:
        domains = [normalize_domain(website_url)]
    client = Client(
        client_id=slug,
        name=name.strip(),
        website_url=website_url or None,
        allowed_domains=domains,
        branding={**DEFAULT_BRANDING, "bot_name": f"{name.strip()} Assistant"},
        crawl_settings=dict(DEFAULT_CRAWL_SETTINGS),
        document_settings=dict(DEFAULT_DOCUMENT_SETTINGS),
        active=True,
    )
    db.add(client)
    db.flush()
    return client


def branding(client: Client) -> dict[str, Any]:
    return {**DEFAULT_BRANDING, **(client.branding or {})}


def crawl_settings(client: Client) -> dict[str, Any]:
    return {**DEFAULT_CRAWL_SETTINGS, **(client.crawl_settings or {})}


def document_settings(client: Client) -> dict[str, Any]:
    return {**DEFAULT_DOCUMENT_SETTINGS, **(client.document_settings or {})}


def validate_branding(changes: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in DEFAULT_BRANDING or key == "logo_file":
            continue
        if key == "primary_color":
            if not isinstance(value, str) or not HEX_COLOR_RE.match(value):
                raise ClientError("Colour must be a hex value like #1f5f5b.")
        elif key == "position":
            if value not in ("left", "right"):
                raise ClientError("Position must be left or right.")
        elif key == "powered_by":
            value = bool(value)
        elif key == "powered_by_url":
            value = normalize_url(value) if value else ""
        else:
            value = str(value or "").strip()[:500]
        clean[key] = value
    return clean


def validate_crawl_settings(changes: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    if "max_pages" in changes:
        clean["max_pages"] = max(1, min(20000, int(changes["max_pages"])))
    if "delay_seconds" in changes:
        clean["delay_seconds"] = max(0.0, min(60.0, float(changes["delay_seconds"])))
    for key in ("include_patterns", "exclude_patterns"):
        if key in changes:
            items = changes[key]
            if isinstance(items, str):
                items = items.splitlines()
            clean[key] = [str(p).strip() for p in items if str(p).strip()][:100]
    return clean


def client_data_dir(slug: str) -> Path:
    """Folder for a client's uploads and branding files (inside DATA_DIR)."""
    return get_settings().data_dir / "clients" / validate_slug(slug)


# ----------------------------------------------------------------------------- views
def client_counts(db: Session, client_ids: list[int] | None = None, days: int = 30) -> dict[int, dict[str, int]]:
    """Pages / documents / chunks / questions (last ``days``) per client id."""
    counts: dict[int, dict[str, int]] = {}

    def _add(model: Any, label: str, extra: Any = None) -> None:
        stmt = select(model.client_id, func.count()).group_by(model.client_id)
        if client_ids is not None:
            stmt = stmt.where(model.client_id.in_(client_ids))
        if extra is not None:
            stmt = stmt.where(extra)
        for cid, n in db.execute(stmt):
            counts.setdefault(cid, {})[label] = n

    _add(Page, "pages")
    _add(Document, "documents")
    _add(Chunk, "chunks")
    _add(Question, "questions_30d", Question.created_at >= func.now() - func.make_interval(0, 0, 0, days))
    return counts


def client_to_dict(client: Client, counts: dict[str, int] | None = None) -> dict[str, Any]:
    counts = counts or {}
    return {
        "id": client.id,
        "client_id": client.client_id,
        "name": client.name,
        "website_url": client.website_url,
        "allowed_domains": client.allowed_domains,
        "active": client.active,
        "ai_model_id": client.ai_model_id,
        "ai_model_name": client.ai_model.name if client.ai_model else None,
        "last_crawl_at": client.last_crawl_at,
        "created_at": client.created_at,
        "branding": branding(client),
        "crawl_settings": crawl_settings(client),
        "document_settings": document_settings(client),
        "api_actions_enabled": client.api_actions_enabled,
        "pages": counts.get("pages", 0),
        "documents": counts.get("documents", 0),
        "chunks": counts.get("chunks", 0),
        "questions_30d": counts.get("questions_30d", 0),
    }


def public_config(client: Client, chat_notice: str, logo_url: str | None) -> dict[str, Any]:
    """Branding for the widget. Contains NO secrets or internal settings."""
    b = branding(client)
    return {
        "client_id": client.client_id,
        "name": client.name,
        "bot_name": b["bot_name"],
        "primary_color": b["primary_color"],
        "greeting": b["greeting"],
        "default_language": b["default_language"],
        "position": b["position"],
        "logo_url": logo_url,
        "notice": chat_notice,
        "powered_by": b["powered_by"],
        "powered_by_text": b["powered_by_text"] if b["powered_by"] else "",
        "powered_by_url": b["powered_by_url"] if b["powered_by"] else "",
    }


# ----------------------------------------------------------------------------- mode
MODES = ("cloud", "onprem")


def switch_mode(db: Session, mode: str) -> None:
    """Switch between cloud (many clients) and on-premise (one client). Caller commits.

    No client data changes. Going on-premise requires at most one client: the remaining
    client becomes the on-premise assistant. Other clients are never deleted or hidden
    implicitly; the admin has to remove them first.
    """
    from app.services.settings import get_setting, set_setting

    if mode not in MODES:
        raise ClientError("Mode must be cloud or onprem.")
    if mode == get_setting(db, "mode"):
        return
    if mode == "cloud":
        set_setting(db, "mode", "cloud")
        set_setting(db, "onprem_client_id", None)
        return
    clients = list(db.scalars(select(Client).order_by(Client.id)))
    if len(clients) > 1:
        names = ", ".join(c.name for c in clients)
        raise ClientError(
            f"Single-client mode allows one client, but there are {len(clients)} ({names}). "
            "Delete the clients you no longer need first."
        )
    set_setting(db, "mode", "onprem")
    set_setting(db, "onprem_client_id", clients[0].id if clients else None)
