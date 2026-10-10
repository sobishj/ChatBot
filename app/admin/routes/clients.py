"""Clients list + client detail tabs (overview, website, documents, AI model, branding,
allowed domains, test chat, embed code, stats, conversations).

Access: super admins see every client; client admins only their assigned clients and
only the tabs they may use (enforced here, not just hidden in the UI).
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.admin.deps import get_client_for_user, require_setup_completed, require_super_admin, require_user
from app.analytics.stats import export_csv, list_conversations, question_stats
from app.chat.service import answer_question
from app.db.models import AIModel, Chunk, Client, Document, Job, Page, User
from app.db.session import get_db, session_scope
from app.documents.service import (
    DocumentError,
    delete_document,
    document_to_dict,
    save_upload,
    uploads_dir,
    validate_watch_path,
)
from app.services import jobs as job_service
from app.services.ai_models import model_to_dict
from app.services.clients import (
    ClientError,
    client_counts,
    client_data_dir,
    client_to_dict,
    crawl_settings,
    create_client,
    document_settings,
    normalize_domains,
    normalize_url,
    validate_branding,
    validate_crawl_settings,
)
from app.services.embeddings import large_upload_warning
from app.services.settings import get_setting, get_settings_map, set_setting
from app.services.users import accessible_client_ids

router = APIRouter(prefix="/api/admin/clients", tags=["clients"], dependencies=[Depends(require_setup_completed)])

LOGO_MAX_BYTES = 1024 * 1024
LOGO_TYPES = {b"\x89PNG": ("png", "image/png"), b"\xff\xd8\xff": ("jpg", "image/jpeg"), b"GIF8": ("gif", "image/gif"), b"RIFF": ("webp", "image/webp")}


def _require_super(user: User) -> None:
    if user.role != "super_admin":
        raise HTTPException(status_code=403, detail="Only super admins can change this.")


def _detail(db: Session, client: Client, user: User) -> dict[str, Any]:
    counts = client_counts(db, [client.id]).get(client.id, {})
    data = client_to_dict(client, counts)
    values = get_settings_map(db, ["default_model_id", "public_domain", "mode"])
    default = db.get(AIModel, values["default_model_id"]) if values["default_model_id"] else None
    data["effective_model_name"] = client.ai_model.name if client.ai_model else (default.name if default else None)
    data["active_jobs"] = [
        job_service.job_to_dict(j)
        for j in db.scalars(select(Job).where(Job.client_id == client.id, Job.status.in_(job_service.ACTIVE_STATUSES)).order_by(Job.id))
    ]
    data["recent_jobs"] = [
        job_service.job_to_dict(j) for j in db.scalars(select(Job).where(Job.client_id == client.id).order_by(Job.id.desc()).limit(10))
    ]
    data["logo_url"] = _admin_logo_url(client)
    data["can_edit_settings"] = user.role == "super_admin"
    data["mode"] = values["mode"]
    return data


def _admin_logo_url(client: Client) -> str | None:
    logo = (client.branding or {}).get("logo_file")
    if not logo:
        return None
    return f"/api/admin/clients/{client.client_id}/logo?v={(client.branding or {}).get('logo_version', 0)}"


# ----------------------------------------------------------------------------- list / create
@router.get("")
def list_clients(user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    allowed = accessible_client_ids(user)
    stmt = select(Client).order_by(Client.name)
    if allowed is not None:
        stmt = stmt.where(Client.id.in_(allowed or [-1]))
    clients = list(db.scalars(stmt))
    counts = client_counts(db, [c.id for c in clients])
    default_id = get_setting(db, "default_model_id")
    default = db.get(AIModel, default_id) if default_id else None
    rows = []
    for c in clients:
        row = client_to_dict(c, counts.get(c.id))
        row["effective_model_name"] = c.ai_model.name if c.ai_model else (f"{default.name} (default)" if default else None)
        rows.append(row)
    return {"clients": rows, "mode": get_setting(db, "mode")}


class ClientIn(BaseModel):
    client_id: str
    name: str
    website_url: str = ""
    allowed_domains: list[str] = []


@router.post("")
def add_client(body: ClientIn, user: User = Depends(require_super_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if get_setting(db, "mode") == "onprem" and db.scalar(select(func.count(Client.id))):
        raise HTTPException(status_code=400, detail="On-premise mode supports a single client.")
    client = create_client(db, body.client_id, body.name, body.website_url, body.allowed_domains)
    if get_setting(db, "mode") == "onprem":
        set_setting(db, "onprem_client_id", client.id)  # the first client becomes the assistant
    db.commit()
    return {"client": _detail(db, client, user)}


# ----------------------------------------------------------------------------- overview
@router.get("/{client_id}")
def get_client(client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"client": _detail(db, client, user)}


class ClientPatch(BaseModel):
    name: str | None = None
    website_url: str | None = None
    active: bool | None = None


@router.patch("/{client_id}")
def update_client(
    body: ClientPatch, client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    _require_super(user)
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="Name is required.")
        client.name = body.name.strip()
    if body.website_url is not None:
        client.website_url = normalize_url(body.website_url) or None
    if body.active is not None:
        client.active = body.active
    db.commit()
    return {"client": _detail(db, client, user)}


@router.delete("/{client_id}")
def remove_client(client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, bool]:
    _require_super(user)
    if get_setting(db, "onprem_client_id") == client.id:
        raise HTTPException(status_code=400, detail="The on-premise client cannot be deleted.")
    folder = client_data_dir(client.client_id)
    db.delete(client)
    db.commit()
    shutil.rmtree(folder, ignore_errors=True)
    return {"ok": True}


# ----------------------------------------------------------------------------- website
@router.post("/{client_id}/crawl")
def crawl_now(client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not client.website_url:
        raise HTTPException(status_code=400, detail="Set the website URL first.")
    job = job_service.enqueue(db, "crawl", client.id, created_by_id=user.id)
    return {"job": job_service.job_to_dict(job)}


class CrawlSettingsIn(BaseModel):
    max_pages: int | None = None
    delay_seconds: float | None = None
    include_patterns: list[str] | str | None = None
    exclude_patterns: list[str] | str | None = None
    extraction: str | None = None


@router.put("/{client_id}/crawl-settings")
def update_crawl_settings(
    body: CrawlSettingsIn, client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    _require_super(user)
    changes = validate_crawl_settings(body.model_dump(exclude_none=True))
    if body.extraction is not None:
        if body.extraction not in ("auto", "main_content"):
            raise HTTPException(status_code=400, detail="Extraction must be auto or main_content.")
        changes["extraction"] = body.extraction
    client.crawl_settings = {**crawl_settings(client), **changes}
    db.commit()
    return {"client": _detail(db, client, user)}


@router.get("/{client_id}/pages")
def list_pages(
    search: str = "",
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    client: Client = Depends(get_client_for_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    chunk_counts = select(Chunk.page_id, func.count(Chunk.id).label("n")).where(Chunk.client_id == client.id).group_by(Chunk.page_id).subquery()
    stmt = (
        select(Page.id, Page.url, Page.title, Page.crawled_at, func.length(Page.content), func.coalesce(chunk_counts.c.n, 0))
        .outerjoin(chunk_counts, chunk_counts.c.page_id == Page.id)
        .where(Page.client_id == client.id)
    )
    if search.strip():
        like = f"%{search.strip()}%"
        stmt = stmt.where(Page.url.ilike(like) | Page.title.ilike(like))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(Page.url).offset((page - 1) * per_page).limit(per_page)).all()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [
            {"id": r[0], "url": r[1], "title": r[2], "crawled_at": r[3], "characters": r[4], "chunks": r[5]} for r in rows
        ],
    }


@router.get("/{client_id}/pages/{page_id}")
def get_page(page_id: int, client: Client = Depends(get_client_for_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    page = db.get(Page, page_id)
    if page is None or page.client_id != client.id:
        raise HTTPException(status_code=404, detail="Page not found.")
    return {"page": {"id": page.id, "url": page.url, "title": page.title, "content": page.content, "crawled_at": page.crawled_at}}


@router.delete("/{client_id}/pages/{page_id}")
def delete_page(page_id: int, client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, bool]:
    _require_super(user)
    page = db.get(Page, page_id)
    if page is None or page.client_id != client.id:
        raise HTTPException(status_code=404, detail="Page not found.")
    db.delete(page)
    db.commit()
    return {"ok": True}


# ----------------------------------------------------------------------------- documents
@router.get("/{client_id}/documents")
def list_documents(client: Client = Depends(get_client_for_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.config import get_settings

    docs = db.scalars(select(Document).where(Document.client_id == client.id).order_by(Document.source, Document.path))
    return {
        "documents": [document_to_dict(d) for d in docs],
        "settings": document_settings(client),
        "watched_root": str(get_settings().watched_dir),
        "mode": get_setting(db, "mode"),
    }


@router.post("/{client_id}/documents")
def upload_documents(
    files: list[UploadFile] = File(...),
    client: Client = Depends(get_client_for_user),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    saved: list[dict[str, Any]] = []
    errors: list[str] = []
    for upload in files:
        try:
            doc = save_upload(db, client, upload.filename or "upload", upload.file)
            saved.append(document_to_dict(doc))
        except DocumentError as exc:
            errors.append(f"{upload.filename}: {exc}")
    # A local model can need hours for very large uploads: hold them and let the admin decide
    # (index anyway, or switch to a much faster cloud model, which re-indexes everything).
    large = large_upload_warning(db, [uploads_dir(client) / d["path"] for d in saved]) if saved else None
    job = None
    if saved and large is None:
        job = job_service.enqueue(db, "index_docs", client.id, payload={"sources": ["upload"]}, created_by_id=user.id)
    return {"saved": saved, "errors": errors, "job": job_service.job_to_dict(job) if job else None, "large": large}


@router.post("/{client_id}/documents/index")
def index_pending_documents(client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Index documents that are waiting (new or changed); unchanged ones are skipped."""
    job = job_service.enqueue(db, "index_docs", client.id, payload={"sources": ["upload"]}, created_by_id=user.id)
    return {"job": job_service.job_to_dict(job)}


@router.delete("/{client_id}/documents/{document_id}")
def remove_document(document_id: int, client: Client = Depends(get_client_for_user), db: Session = Depends(get_db)) -> dict[str, bool]:
    doc = db.get(Document, document_id)
    if doc is None or doc.client_id != client.id:
        raise HTTPException(status_code=404, detail="Document not found.")
    try:
        delete_document(db, doc, client)
    except DocumentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


class DocEnabledIn(BaseModel):
    enabled: bool


@router.put("/{client_id}/documents/{document_id}/enabled")
def set_document_enabled(
    document_id: int, body: DocEnabledIn, client: Client = Depends(get_client_for_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Use or ignore one document for answers. It stays indexed, so switching back is instant."""
    doc = db.get(Document, document_id)
    if doc is None or doc.client_id != client.id:
        raise HTTPException(status_code=404, detail="Document not found.")
    doc.enabled = body.enabled
    db.commit()
    return {"document": document_to_dict(doc)}


@router.post("/{client_id}/documents/reindex")
def reindex_documents(client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    job = job_service.enqueue(db, "index_docs", client.id, payload={"force": True}, created_by_id=user.id)
    return {"job": job_service.job_to_dict(job)}


@router.post("/{client_id}/documents/scan")
def scan_folder(client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not document_settings(client).get("watch_path"):
        raise HTTPException(status_code=400, detail="Set a watched folder first.")
    job = job_service.enqueue(db, "scan_folder", client.id, created_by_id=user.id)
    return {"job": job_service.job_to_dict(job)}


class DocSettingsIn(BaseModel):
    watch_path: str | None = None
    scan_interval_minutes: int | None = Field(default=None, ge=5, le=10080)


class DocAnsweringIn(BaseModel):
    answer_any_topic: bool


@router.put("/{client_id}/documents/answering")
def update_document_answering(body: DocAnsweringIn, client: Client = Depends(get_client_for_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Whether uploaded documents may answer questions unrelated to the business (client admins may change it)."""
    client.document_settings = {**document_settings(client), "answer_any_topic": body.answer_any_topic}
    db.commit()
    return {"settings": document_settings(client)}


@router.put("/{client_id}/documents/settings")
def update_document_settings(
    body: DocSettingsIn, client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    _require_super(user)
    settings = document_settings(client)
    if body.watch_path is not None:
        if body.watch_path.strip():
            try:
                settings["watch_path"] = validate_watch_path(body.watch_path)
            except DocumentError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            settings["watch_path"] = None
    if body.scan_interval_minutes is not None:
        settings["scan_interval_minutes"] = body.scan_interval_minutes
    client.document_settings = settings
    db.commit()
    job = None
    if settings.get("watch_path"):
        job = job_service.enqueue(db, "scan_folder", client.id, created_by_id=user.id)
    else:
        # Folder removed: drop its documents (and chunks).
        for doc in db.scalars(select(Document).where(Document.client_id == client.id, Document.source == "folder")):
            db.delete(doc)
        db.commit()
    return {"settings": settings, "job": job_service.job_to_dict(job) if job else None}


# ----------------------------------------------------------------------------- AI model
class ModelChoiceIn(BaseModel):
    ai_model_id: int | None


@router.get("/{client_id}/model")
def get_model_choice(client: Client = Depends(get_client_for_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    values = get_settings_map(db, ["default_model_id", "fallback_model_id", "mode"])
    models = list(db.scalars(select(AIModel).order_by(AIModel.name)))
    return {
        "ai_model_id": client.ai_model_id,
        "default_model_id": values["default_model_id"],
        "fallback_model_id": values["fallback_model_id"],
        "mode": values["mode"],
        "models": [
            {k: v for k, v in model_to_dict(m, values["default_model_id"], values["fallback_model_id"]).items() if k not in ("api_key_masked",)}
            for m in models
        ],
    }


@router.put("/{client_id}/model")
def set_model_choice(
    body: ModelChoiceIn, client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    _require_super(user)
    if body.ai_model_id is not None and db.get(AIModel, body.ai_model_id) is None:
        raise HTTPException(status_code=404, detail="Model not found.")
    client.ai_model_id = body.ai_model_id
    db.commit()
    db.refresh(client)
    return {"client": _detail(db, client, user)}


# ----------------------------------------------------------------------------- branding
@router.put("/{client_id}/branding")
def update_branding(
    body: dict[str, Any], client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    changes = validate_branding(body)
    client.branding = {**(client.branding or {}), **changes}
    db.commit()
    return {"client": _detail(db, client, user)}


@router.post("/{client_id}/logo")
def upload_logo(
    file: UploadFile = File(...), client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    data = file.file.read(LOGO_MAX_BYTES + 1)
    if len(data) > LOGO_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Logo must be 1 MB or smaller.")
    kind = next((v for magic, v in LOGO_TYPES.items() if data.startswith(magic)), None)
    if kind is None or (kind[0] == "webp" and data[8:12] != b"WEBP"):
        raise HTTPException(status_code=400, detail="Upload a PNG, JPG, GIF or WebP image.")
    folder = client_data_dir(client.client_id) / "branding"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("logo.*"):
        old.unlink(missing_ok=True)
    filename = f"logo.{kind[0]}"
    (folder / filename).write_bytes(data)
    client.branding = {**(client.branding or {}), "logo_file": filename, "logo_version": int(time.time())}
    db.commit()
    return {"client": _detail(db, client, user)}


@router.delete("/{client_id}/logo")
def delete_logo(client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    folder = client_data_dir(client.client_id) / "branding"
    for old in folder.glob("logo.*"):
        old.unlink(missing_ok=True)
    client.branding = {**(client.branding or {}), "logo_file": None}
    db.commit()
    return {"client": _detail(db, client, user)}


@router.get("/{client_id}/logo")
def get_logo(client: Client = Depends(get_client_for_user)) -> FileResponse:
    return logo_response(client)


def logo_response(client: Client) -> FileResponse:
    filename = (client.branding or {}).get("logo_file")
    path = client_data_dir(client.client_id) / "branding" / filename if filename else None
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="No logo.")
    media = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}.get(path.suffix[1:], "application/octet-stream")
    return FileResponse(
        path,
        media_type=media,
        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"},
    )


# ----------------------------------------------------------------------------- domains
class DomainsIn(BaseModel):
    allowed_domains: list[str]


@router.put("/{client_id}/domains")
def update_domains(
    body: DomainsIn, client: Client = Depends(get_client_for_user), user: User = Depends(require_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    _require_super(user)
    try:
        client.allowed_domains = normalize_domains(body.allowed_domains)
    except ClientError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"client": _detail(db, client, user)}


# ----------------------------------------------------------------------------- test chat
class TestChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=1000)
    confirm: bool | None = None


@router.post("/{client_id}/test-chat")
def test_chat(body: TestChatIn, client: Client = Depends(get_client_for_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Chat exactly like a visitor (same pipeline), with diagnostics. Logged with channel 'test'."""
    result = answer_question(db, client, body.session_id, body.message, channel="test", confirm=body.confirm)
    return {
        "confirmation": result.confirmation,
        "action_results": result.action_results,
        "answer": result.answer,
        "sources": result.sources,
        "answered": result.answered,
        "confidence": result.confidence,
        "threshold": get_setting(db, "confidence_threshold"),
        "model_name": result.model_name,
        "used_fallback": result.used_fallback,
        "response_ms": result.response_ms,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cost": result.cost,
        "language": result.language,
        "error": result.error,
        "chunks": [
            {
                "title": h.title,
                "source": h.source,
                "source_type": h.source_type,
                "similarity": h.similarity,
                "keyword": h.text_rank is not None,
                "preview": h.content[:300],
            }
            for h in result.hits
        ],
    }


# ----------------------------------------------------------------------------- embed code
@router.get("/{client_id}/embed")
def embed_code(request: Request, client: Client = Depends(get_client_for_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.config import get_settings

    domain = get_setting(db, "public_domain")
    if domain:
        scheme = "http" if domain.startswith(("localhost", "127.")) else "https"
        base = f"{scheme}://{domain}"
    else:
        base = f"{request.url.scheme}://{request.url.hostname}:{get_settings().app_port}"
    snippet = f'<script src="{base}/widget.js" data-client="{client.client_id}" defer></script>'
    return {"snippet": snippet, "widget_url": f"{base}/widget.js", "public_domain": domain, "allowed_domains": client.allowed_domains}


# ----------------------------------------------------------------------------- stats + conversations
@router.get("/{client_id}/stats")
def client_stats(
    days: int = Query(30, ge=1, le=365), include_test: bool = False, client: Client = Depends(get_client_for_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return question_stats(db, [client.id], days=days, include_test=include_test)


@router.get("/{client_id}/conversations")
def conversations(
    search: str = "",
    answered: str = Query("all", pattern="^(all|yes|no)$"),
    channel: str = Query("all", pattern="^(all|widget|test|cli)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    client: Client = Depends(get_client_for_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return list_conversations(db, client.id, page=page, per_page=per_page, search=search, answered=answered, channel=channel)


@router.get("/{client_id}/conversations.csv")
def conversations_csv(
    search: str = "",
    answered: str = Query("all", pattern="^(all|yes|no)$"),
    channel: str = Query("all", pattern="^(all|widget|test|cli)$"),
    client: Client = Depends(get_client_for_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    filename = f"conversations-{client.client_id}-{time.strftime('%Y%m%d')}.csv"
    client_pk = client.id

    def stream() -> Iterator[str]:
        # Own session: the request's session is closed before a streaming body is sent.
        with session_scope() as export_db:
            yield from export_csv(export_db, client_pk, search=search, answered=answered, channel=channel)

    return StreamingResponse(
        stream(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{client_id}/jobs")
def client_jobs(limit: int = Query(20, ge=1, le=200), client: Client = Depends(get_client_for_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    jobs = db.scalars(select(Job).where(Job.client_id == client.id).order_by(Job.id.desc()).limit(limit))
    return {"jobs": [job_service.job_to_dict(j) for j in jobs]}
