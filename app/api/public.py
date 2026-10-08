"""Public endpoints on APP_PORT (used by the widget on client websites).

Security model
--------------
* Every chat/config request must come from a page whose origin is in the client's
  ``allowed_domains`` (Origin header, or Referer when a browser omits Origin).
  Requests from other sites — or with no origin at all, e.g. curl — are rejected.
* CORS headers echo only allowed origins; preflights are answered for origins that
  belong to any active client (the body, and so the client id, isn't sent in a preflight).
* Rate limits per IP and per client (Postgres counters, shared across processes).
* Nothing secret is ever returned: config contains branding only.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat.service import answer_question
from app.db.models import Client
from app.db.session import get_db, session_scope
from app.services import rate_limit
from app.services.clients import get_by_slug, origin_allowed, public_config
from app.services.settings import get_setting

router = APIRouter(tags=["public"])

WIDGET_FILE = Path(__file__).resolve().parents[1] / "widget" / "widget.js"
_ORIGIN_CACHE_SECONDS = 30.0
_origin_cache: dict[str, Any] = {"at": 0.0, "domains": []}


def request_origin(request: Request) -> str | None:
    """The page origin: Origin header, or scheme://host[:port] of the Referer."""
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return origin
    referer = request.headers.get("referer")
    if referer:
        p = urlparse(referer)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    return None


def _all_allowed_domains() -> list[str]:
    """Allowed domains of all active clients (cached briefly; used for CORS preflights)."""
    now = time.monotonic()
    if now - _origin_cache["at"] > _ORIGIN_CACHE_SECONDS:
        with session_scope() as db:
            domains: list[str] = []
            for allowed in db.scalars(select(Client.allowed_domains).where(Client.active.is_(True))):
                domains.extend(allowed or [])
        _origin_cache.update(at=now, domains=domains)
    return list(_origin_cache["domains"])


def cors_headers(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


def _client_for_origin(db: Session, client_id: str, request: Request) -> tuple[Client, str]:
    """Return the active client and the verified origin, or raise 403/404."""
    client = get_by_slug(db, client_id)
    if client is None or not client.active:
        raise HTTPException(status_code=404, detail="Unknown client.")
    origin = request_origin(request)
    if not origin_allowed(origin, client.allowed_domains or []):
        raise HTTPException(status_code=403, detail="This website is not allowed to use this assistant.")
    assert origin is not None
    return client, origin


@router.options("/api/chat")
@router.options("/api/client/{client_id}/config")
def preflight(request: Request) -> Response:
    origin = request.headers.get("origin")
    if origin and origin_allowed(origin, _all_allowed_domains()):
        return Response(status_code=204, headers=cors_headers(origin))
    return Response(status_code=403)


@router.get("/api/client/{client_id}/config")
def client_config(client_id: str, request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    client, origin = _client_for_origin(db, client_id, request)
    logo = (client.branding or {}).get("logo_file")
    logo_url = f"/api/client/{client.client_id}/logo?v={(client.branding or {}).get('logo_version', 0)}" if logo else None
    config = public_config(client, get_setting(db, "chat_notice") or "", logo_url)
    return JSONResponse(config, headers={**cors_headers(origin), "Cache-Control": "no-cache"})


@router.get("/api/client/{client_id}/logo")
def client_logo(client_id: str, db: Session = Depends(get_db)) -> FileResponse:
    from app.admin.routes.clients import logo_response

    client = get_by_slug(db, client_id)
    if client is None or not client.active:
        raise HTTPException(status_code=404, detail="Not found.")
    return logo_response(client)


class ChatIn(BaseModel):
    client_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    message: str = Field(min_length=1, max_length=1000)


@router.post("/api/chat")
def chat(body: ChatIn, request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    client, origin = _client_for_origin(db, body.client_id, request)
    headers = cors_headers(origin)
    limits = get_setting(db, "rate_limits") or {}
    ip = request.client.host if request.client else "unknown"
    if not rate_limit.hit(db, f"chat:ip:{ip}", int(limits.get("chat_per_ip_per_minute", 20)), 60) or not rate_limit.hit(
        db, f"chat:client:{client.id}", int(limits.get("chat_per_client_per_minute", 300)), 60
    ):
        return JSONResponse({"detail": "Too many messages. Please wait a moment and try again."}, status_code=429, headers=headers)
    if not body.message.strip():
        return JSONResponse({"detail": "Empty message."}, status_code=400, headers=headers)
    result = answer_question(db, client, body.session_id, body.message, channel="widget")
    return JSONResponse({"answer": result.answer, "sources": result.sources, "answered": result.answered}, headers=headers)


@router.get("/widget.js")
def widget_js() -> FileResponse:
    return FileResponse(
        WIDGET_FILE,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*", "X-Content-Type-Options": "nosniff"},
    )
