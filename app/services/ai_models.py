"""Saved AI models: CRUD, connection tests and per-client model resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AIModel, Client
from app.llm.client import LLMError, LLMResult, ModelConfig, list_available_models, test_connection
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
    set_priority(db, [i for i in model_priority(db) if i != model.id])
    db.delete(model)
    db.flush()


# ----------------------------------------------------------------------------- priority
def model_priority(db: Session) -> list[int]:
    """Saved model ids in the order chat tries them: the default first, then the priority list,
    then the fallback (older setups and the setup wizard only set default and fallback)."""
    default, fallback = get_setting(db, "default_model_id"), get_setting(db, "fallback_model_id")
    order = [default, *(get_setting(db, "model_priority") or []), fallback]
    existing = set(db.scalars(select(AIModel.id)))
    return list(dict.fromkeys(i for i in order if i in existing))


def set_priority(db: Session, model_ids: list[int]) -> list[int]:
    """Store the order; the first model is the default and the second the fallback (kept for older screens)."""
    existing = set(db.scalars(select(AIModel.id)))
    order = list(dict.fromkeys(int(i) for i in model_ids if int(i) in existing))
    set_setting(db, "model_priority", order)
    set_setting(db, "default_model_id", order[0] if order else None)
    set_setting(db, "fallback_model_id", order[1] if len(order) > 1 else None)
    return order


def resolve_chain(db: Session, client: Client) -> list[ModelConfig]:
    """Models to try for a client, in order: its own model (if set), then the priority list."""
    ids = ([client.ai_model_id] if client.ai_model_id else []) + model_priority(db)
    rows = [db.get(AIModel, i) for i in dict.fromkeys(ids)]
    return [ModelConfig.from_row(r) for r in rows if r is not None]


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


def discover_models(data: dict[str, Any], existing: AIModel | None = None) -> dict[str, Any]:
    """List the models the form's provider/URL/key can use; the model name may still be empty."""
    provider = str(data.get("provider") or (existing.provider if existing else "") or "")
    try:
        get_provider(provider)
    except ValueError as exc:
        raise ModelError(str(exc)) from exc
    base_url = str(data.get("base_url") or "").strip() or None
    api_key = (data.get("api_key") or "").strip() or None
    if api_key is None and existing is not None and existing.api_key_encrypted:
        api_key = decrypt_secret(existing.api_key_encrypted)
    try:
        models = list_available_models(provider, base_url, api_key, use_cache=not data.get("refresh"))
        return {"ok": True, "models": models}
    except LLMError as exc:
        return {"ok": False, "error": str(exc)}


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
    """(first, second) of :func:`resolve_chain`, for code that only needs two."""
    chain = resolve_chain(db, client)
    return (chain[0] if chain else None, chain[1] if len(chain) > 1 else None)
