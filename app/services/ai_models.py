"""Saved AI models: CRUD, connection tests and per-client model resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AIModel, Client
from app.llm.client import LLMError, LLMResult, ModelConfig, test_connection
from app.llm.providers import PROVIDERS, get_provider, is_loopback_url
from app.security.crypto import decrypt_secret, encrypt_secret, mask_secret
from app.services.settings import get_setting, set_setting


class ModelError(ValueError):
    """Validation error with a message safe to show in the UI."""


EDITABLE_FIELDS = ("name", "provider", "base_url", "model_name", "temperature", "max_tokens", "timeout_seconds", "cost_input_per_m", "cost_output_per_m")


def validate_fields(data: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    if "provider" in data:
        get_provider(str(data["provider"]))  # raises ValueError for unknown providers
        clean["provider"] = str(data["provider"])
    for key in ("name", "model_name"):
        if key in data:
            value = str(data[key] or "").strip()
            if not value:
                raise ModelError(f"{'Display name' if key == 'name' else 'Model name'} is required.")
            clean[key] = value[:300]
    if "base_url" in data:
        value = str(data["base_url"] or "").strip()
        if value and not value.startswith(("http://", "https://")):
            raise ModelError("Base URL must start with http:// or https://")
        clean["base_url"] = value or None
    if "temperature" in data:
        clean["temperature"] = max(0.0, min(2.0, float(data["temperature"])))
    if "max_tokens" in data:
        clean["max_tokens"] = max(16, min(32000, int(data["max_tokens"])))
    if "timeout_seconds" in data:
        clean["timeout_seconds"] = max(5, min(600, int(data["timeout_seconds"])))
    for key in ("cost_input_per_m", "cost_output_per_m"):
        if key in data:
            v = data[key]
            clean[key] = None if v in (None, "") else max(0.0, float(v))
    return clean


def config_from_form(data: dict[str, Any], existing: AIModel | None = None) -> ModelConfig:
    """Build a call config from form data, reusing the stored key when the field is left blank."""
    fields = validate_fields(data)
    merged: dict[str, Any] = {}
    if existing is not None:
        merged = {k: getattr(existing, k) for k in EDITABLE_FIELDS}
    merged.update(fields)
    api_key = (data.get("api_key") or "").strip() or None
    if api_key is None and existing is not None and existing.api_key_encrypted:
        api_key = decrypt_secret(existing.api_key_encrypted)
    if not merged.get("provider") or not merged.get("model_name"):
        raise ModelError("Provider and model name are required.")
    return ModelConfig(
        provider=merged["provider"],
        model_name=merged["model_name"],
        base_url=merged.get("base_url"),
        api_key=api_key,
        temperature=merged.get("temperature", 0.2),
        max_tokens=merged.get("max_tokens", 600),
        timeout_seconds=merged.get("timeout_seconds", 60),
        cost_input_per_m=merged.get("cost_input_per_m"),
        cost_output_per_m=merged.get("cost_output_per_m"),
        model_id=existing.id if existing else None,
        display_name=merged.get("name") or merged["model_name"],
    )


def save_model(db: Session, data: dict[str, Any], existing: AIModel | None = None) -> AIModel:
    fields = validate_fields(data)
    model = existing or AIModel(temperature=0.2, max_tokens=600, timeout_seconds=60)
    for key, value in fields.items():
        setattr(model, key, value)
    if not model.name or not model.provider or not model.model_name:
        raise ModelError("Display name, provider and model name are required.")
    api_key = (data.get("api_key") or "").strip()
    if api_key:
        model.api_key_encrypted = encrypt_secret(api_key)
    elif data.get("clear_api_key"):
        model.api_key_encrypted = None
    if existing is None:
        db.add(model)
    db.flush()
    # The first saved model becomes the default automatically.
    if get_setting(db, "default_model_id") is None:
        set_setting(db, "default_model_id", model.id)
    return model


def delete_model(db: Session, model: AIModel) -> None:
    for key in ("default_model_id", "fallback_model_id"):
        if get_setting(db, key) == model.id:
            set_setting(db, key, None)
    db.delete(model)
    db.flush()


def record_test(db: Session, model: AIModel, ok: bool, message: str) -> None:
    model.last_test_ok = ok
    model.last_test_at = datetime.now(UTC)
    model.last_test_message = message[:1000]
    db.flush()


def run_test(cfg: ModelConfig) -> dict[str, Any]:
    """Run a connection test and return a UI-friendly result dict."""
    try:
        result: LLMResult = test_connection(cfg)
    except LLMError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "reply": result.text,
        "response_ms": result.response_ms,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }


def model_to_dict(model: AIModel, default_id: int | None = None, fallback_id: int | None = None) -> dict[str, Any]:
    provider = PROVIDERS.get(model.provider)
    key = decrypt_secret(model.api_key_encrypted) if model.api_key_encrypted else None
    return {
        "id": model.id,
        "name": model.name,
        "provider": model.provider,
        "provider_label": provider.label if provider else model.provider,
        "cloud": provider.cloud if provider else True,
        "base_url": model.base_url,
        "model_name": model.model_name,
        "api_key_masked": mask_secret(key),
        "has_api_key": bool(model.api_key_encrypted),
        "api_key_unreadable": bool(model.api_key_encrypted) and key is None,
        "temperature": model.temperature,
        "max_tokens": model.max_tokens,
        "timeout_seconds": model.timeout_seconds,
        "cost_input_per_m": model.cost_input_per_m,
        "cost_output_per_m": model.cost_output_per_m,
        "is_default": model.id == default_id,
        "is_fallback": model.id == fallback_id,
        "loopback_warning": is_loopback_url(model.base_url),
        "last_test_ok": model.last_test_ok,
        "last_test_at": model.last_test_at,
        "last_test_message": model.last_test_message,
    }


def list_models(db: Session) -> list[AIModel]:
    return list(db.scalars(select(AIModel).order_by(AIModel.name)))


def resolve_for_client(db: Session, client: Client) -> tuple[ModelConfig | None, ModelConfig | None]:
    """Return (primary, fallback) configs for a client: its own model or the system default."""
    primary_row = client.ai_model
    if primary_row is None:
        default_id = get_setting(db, "default_model_id")
        primary_row = db.get(AIModel, default_id) if default_id else None
    fallback_id = get_setting(db, "fallback_model_id")
    fallback_row = db.get(AIModel, fallback_id) if fallback_id else None
    if fallback_row is not None and primary_row is not None and fallback_row.id == primary_row.id:
        fallback_row = None
    return (
        ModelConfig.from_row(primary_row) if primary_row else None,
        ModelConfig.from_row(fallback_row) if fallback_row else None,
    )
