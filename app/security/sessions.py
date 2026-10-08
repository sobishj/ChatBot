"""Signed, HttpOnly session cookies and double-submit CSRF tokens for the admin UI."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import get_settings

SESSION_COOKIE = "wa_session"
CSRF_COOKIE = "wa_csrf"
CSRF_HEADER = "x-csrf-token"
SESSION_MAX_AGE = 12 * 3600  # seconds


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="website-assistant:session")


def is_https(request: Request) -> bool:
    """True when the browser reached us over HTTPS (directly or via a trusted proxy)."""
    return request.url.scheme == "https"


def issue_session(response: Response, request: Request, user_id: int, session_version: int) -> None:
    token = _serializer().dumps({"u": user_id, "v": session_version})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=is_https(request),  # Secure cookies don't work over plain-HTTP LAN installs
        samesite="lax",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def read_session(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    return data if isinstance(data, dict) and "u" in data and "v" in data else None


def ensure_csrf_cookie(request: Request, response: Response) -> None:
    """Give the browser a CSRF token cookie (readable by JS, echoed back in a header)."""
    if request.cookies.get(CSRF_COOKIE):
        return
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(32),
        max_age=30 * 24 * 3600,
        httponly=False,
        secure=is_https(request),
        samesite="strict",
        path="/",
    )


def csrf_valid(request: Request) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    return bool(cookie and header and secrets.compare_digest(cookie, header))
