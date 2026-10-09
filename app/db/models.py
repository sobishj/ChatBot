"""SQLAlchemy ORM models: the single Postgres database holds everything.

Notes
-----
* ``clients.client_id`` is the public slug (used in the embed code). Every other
  table's ``client_id`` column is an integer foreign key to ``clients.id``.
* ``chunks.embedding`` is NOT mapped here. Its vector dimension depends on the
  embedding model chosen in the setup wizard, so the column (and its HNSW index)
  is created at runtime by :mod:`app.indexer.schema` and accessed with SQL.
* ``chunks.content_tsv`` is a generated column (``simple`` text-search config,
  so it works for every language), maintained by Postgres itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all models."""

    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _now_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------- settings
class Setting(Base):
    """Key/value system settings edited in the admin UI (see app.services.settings)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- users
class User(Base):
    """Admin UI user. Roles: ``super_admin`` (everything) or ``client_admin`` (assigned clients)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)  # stored lower-case
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="client_admin")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Bumped on password change / disable to invalidate existing sessions.
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_column()

    clients: Mapped[list[Client]] = relationship(secondary="user_clients", lazy="selectin")


class UserClient(Base):
    """Which clients a ``client_admin`` may manage."""

    __tablename__ = "user_clients"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True)


# --------------------------------------------------------------------------- AI models
class AIModel(Base):
    """An LLM endpoint callable through LiteLLM. Default/fallback ids live in settings."""

    __tablename__ = "ai_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)  # display name
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # key in app.llm.providers
    base_url: Mapped[str | None] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(String(300), nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)  # Fernet token, never sent to the UI
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.2)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    cost_input_per_m: Mapped[float | None] = mapped_column(Float)  # USD per 1M input tokens
    cost_output_per_m: Mapped[float | None] = mapped_column(Float)
    # Result of the most recent "Test connection" (shown on System health).
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- clients
class Client(Base):
    """A business whose website uses the assistant."""

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # public slug
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    website_url: Mapped[str | None] = mapped_column(Text)
    allowed_domains: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    branding: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    crawl_settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Watched folder: {"path": "/mnt/watched/...", "scan_interval_minutes": 60, "last_scan_at": ...}
    document_settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ai_model_id: Mapped[int | None] = mapped_column(ForeignKey("ai_models.id", ondelete="SET NULL"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_crawl_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_column()
    # API actions (off by default). Settings: {"base_url", "auth_type": header|bearer|query, "auth_name",
    # "api_key_encrypted", "timeout_seconds", "extra_headers": {}, "health_path"}. The key never leaves the server.
    api_actions_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    api_settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    ai_model: Mapped[AIModel | None] = relationship(lazy="joined")


class ClientAction(Base):
    """One call the assistant may make to a client's API (e.g. get_slots, book_appointment)."""

    __tablename__ = "client_actions"
    __table_args__ = (UniqueConstraint("client_id", "name", name="uq_client_actions_client_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)  # snake_case tool name
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")  # tells the model when to use it
    method: Mapped[str] = mapped_column(String(10), nullable=False, default="GET")
    path: Mapped[str] = mapped_column(Text, nullable=False)  # relative to base_url, may contain {placeholders}
    # [{"name", "type", "description", "required", "location": path|query|body, "enum": [...]}]
    parameters: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    response_hint: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ActionCall(Base):
    """Audit log of calls to client APIs. ``request_summary`` has personal data masked."""

    __tablename__ = "action_calls"
    __table_args__ = (Index("ix_action_calls_client_created", "client_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    question_id: Mapped[int | None] = mapped_column(ForeignKey("questions.id", ondelete="SET NULL"))
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(10), nullable=False, default="widget")  # widget|test|admin
    action_name: Mapped[str] = mapped_column(String(64), nullable=False)
    request_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status_code: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    response_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _now_column()


class ActionSession(Base):
    """Per-conversation action state, encrypted: personal-data placeholders and a pending confirmation."""

    __tablename__ = "action_sessions"

    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    vault_encrypted: Mapped[str | None] = mapped_column(Text)  # {"[phone_1]": "+9198..."}
    pending_encrypted: Mapped[str | None] = mapped_column(Text)  # {"action", "params", "summary"}
    pending_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- content
class Page(Base):
    """A crawled web page. ``content`` keeps the extracted text so we can re-index without re-crawling."""

    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("client_id", "url", name="uq_pages_client_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    crawled_at: Mapped[datetime] = _now_column()


class Document(Base):
    """A client document, uploaded in the UI or found in a watched folder."""

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("client_id", "source", "path", name="uq_documents_client_source_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(10), nullable=False)  # 'upload' | 'folder'
    path: Mapped[str] = mapped_column(Text, nullable=False)  # relative to the source root
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash: Mapped[str | None] = mapped_column(String(64))  # hash of the last *indexed* version
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")  # pending|indexed|error
    error: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_at: Mapped[datetime] = _now_column()
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Chunk(Base):
    """A searchable piece of a page or document. See module docstring for ``embedding``."""

    __tablename__ = "chunks"
    __table_args__ = (Index("ix_chunks_content_tsv", "content_tsv", postgresql_using="gin"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(10), nullable=False)  # 'web' | 'doc'
    source: Mapped[str] = mapped_column(Text, nullable=False)  # URL or document filename
    page_id: Mapped[int | None] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', coalesce(title, '') || ' ' || content)", persisted=True),
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = _now_column()


# --------------------------------------------------------------------------- analytics
class Question(Base):
    """One visitor question and the answer given. Personal data is masked before saving."""

    __tablename__ = "questions"
    __table_args__ = (
        Index("ix_questions_client_created", "client_id", "created_at"),
        Index("ix_questions_session", "client_id", "session_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(10), nullable=False, default="widget")  # widget|test|cli
    question: Mapped[str] = mapped_column(Text, nullable=False)
    question_normalized: Mapped[str] = mapped_column(Text, nullable=False)  # for "top questions"
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sources: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    answered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    top_score: Mapped[float | None] = mapped_column(Float)
    language: Mapped[str | None] = mapped_column(String(16))
    model_id: Mapped[int | None] = mapped_column(ForeignKey("ai_models.id", ondelete="SET NULL"))
    model_name: Mapped[str | None] = mapped_column(String(300))  # snapshot, survives model deletion
    used_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # USD estimate
    response_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    actions_used: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    created_at: Mapped[datetime] = _now_column()


# --------------------------------------------------------------------------- jobs
class Job(Base):
    """A background job processed by the worker (crawl, index, scan, re-index, downloads)."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    # queued | running | succeeded | failed | cancelled
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0-100
    progress_text: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    log: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    triggered_by: Mapped[str] = mapped_column(String(20), nullable=False, default="user")  # user|schedule|system
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now_column()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    client: Mapped[Client | None] = relationship(lazy="joined")


# --------------------------------------------------------------------------- rate limits
class RateLimitCounter(Base):
    """Fixed-window request counters shared by all processes (chat + login rate limits)."""

    __tablename__ = "rate_limits"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
