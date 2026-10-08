"""Bootstrap configuration read from environment variables / .env.

Only infrastructure settings live here. Everything a user can change (mode,
public domain, AI models, clients, ...) is stored in the database and managed
from the admin UI.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-level settings. Values come from the environment (or a local .env)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(description="SQLAlchemy URL, e.g. postgresql+psycopg://user:pw@db:5432/chatbot")
    secret_key: str = Field(min_length=32, description="Encrypts API keys and signs sessions")
    app_port: int = Field(default=8000, ge=1, le=65535, description="Public chat API + widget")
    admin_port: int = Field(default=8001, ge=1, le=65535, description="Admin UI")
    data_dir: Path = Field(default=Path("/data"), description="Uploads, logos, model cache")
    watched_dir: Path = Field(default=Path("/mnt/watched"), description="Root of watched document folders")
    forwarded_allow_ips: str = Field(default="127.0.0.1", description="Trusted proxy IPs for X-Forwarded-*")
    log_level: str = Field(default="INFO")


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance (call ``get_settings.cache_clear()`` in tests)."""
    return Settings()  # type: ignore[call-arg]  # required fields come from the environment
