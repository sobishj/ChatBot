"""Thin wrapper around ``litellm.completion`` used for every model call.

All configuration comes from an :class:`~app.db.models.AIModel` row, so adding or
switching models never needs a restart or code change.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from app.db.models import AIModel
from app.llm.providers import get_provider
from app.security.crypto import decrypt_secret

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when a model call fails (network, auth, timeout, bad response)."""


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    response_ms: int
    cost: float
    model_id: int | None
    model_name: str


@dataclass
class ModelConfig:
    """Everything needed to call a model, decoupled from the ORM row (also used for unsaved test configs)."""

    provider: str
    model_name: str
    base_url: str | None = None
    api_key: str | None = None
    temperature: float = 0.2
    max_tokens: int = 600
    timeout_seconds: int = 60
    cost_input_per_m: float | None = None
    cost_output_per_m: float | None = None
    model_id: int | None = None
    display_name: str = ""

    @classmethod
    def from_row(cls, row: AIModel) -> ModelConfig:
        return cls(
            provider=row.provider,
            model_name=row.model_name,
            base_url=row.base_url,
            api_key=decrypt_secret(row.api_key_encrypted) if row.api_key_encrypted else None,
            temperature=row.temperature,
            max_tokens=row.max_tokens,
            timeout_seconds=row.timeout_seconds,
            cost_input_per_m=row.cost_input_per_m,
            cost_output_per_m=row.cost_output_per_m,
            model_id=row.id,
            display_name=row.name,
        )


def _litellm_kwargs(cfg: ModelConfig) -> dict[str, Any]:
    provider = get_provider(cfg.provider)
    kwargs: dict[str, Any] = {"model": f"{provider.litellm_prefix}/{cfg.model_name}"}
    base_url = (cfg.base_url or "").strip()
    if provider.openai_compatible or (base_url and base_url.rstrip("/") != provider.default_base_url.rstrip("/")):
        if base_url:
            kwargs["api_base"] = base_url
    # OpenAI-compatible local servers usually ignore the key, but the client library requires one.
    kwargs["api_key"] = cfg.api_key or ("not-needed" if provider.openai_compatible else None)
    return kwargs


def estimate_cost(cfg: ModelConfig, input_tokens: int, output_tokens: int, response: Any = None) -> float:
    """Use the admin-entered prices when set, otherwise LiteLLM's price list (0 if unknown)."""
    if cfg.cost_input_per_m is not None or cfg.cost_output_per_m is not None:
        return (input_tokens * (cfg.cost_input_per_m or 0) + output_tokens * (cfg.cost_output_per_m or 0)) / 1_000_000
    if response is None:
        return 0.0
    try:
        import litellm

        return float(litellm.completion_cost(completion_response=response) or 0.0)
    except Exception:  # noqa: BLE001 - unknown model / local model: no price information
        return 0.0


def complete(cfg: ModelConfig, messages: list[dict[str, str]], max_tokens: int | None = None) -> LLMResult:
    """Call the model and return text + usage. Raises :class:`LLMError` on any failure."""
    import litellm  # imported lazily: it is slow to import

    litellm.telemetry = False
    litellm.suppress_debug_info = True

    started = time.perf_counter()
    try:
        response = litellm.completion(
            messages=messages,
            temperature=cfg.temperature,
            max_tokens=max_tokens or cfg.max_tokens,
            timeout=cfg.timeout_seconds,
            num_retries=0,
            **_litellm_kwargs(cfg),
        )
    except Exception as exc:  # noqa: BLE001 - LiteLLM raises many exception types
        raise LLMError(_friendly_error(exc)) from exc
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    try:
        text = response.choices[0].message.content or ""
    except (AttributeError, IndexError) as exc:
        raise LLMError("The model returned an unexpected response.") from exc

    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    return LLMResult(
        text=text.strip(),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        response_ms=elapsed_ms,
        cost=estimate_cost(cfg, input_tokens, output_tokens, response),
        model_id=cfg.model_id,
        model_name=cfg.display_name or cfg.model_name,
    )


def test_connection(cfg: ModelConfig) -> LLMResult:
    """Send a tiny prompt to verify URL, model name and API key."""
    return complete(
        cfg,
        [
            {"role": "system", "content": "You are a connection test. Reply with one short sentence."},
            {"role": "user", "content": "Say hello and state which model you are."},
        ],
        max_tokens=60,
    )


def _friendly_error(exc: Exception) -> str:
    """Turn LiteLLM/HTTP exceptions into a short message for the admin UI (no secrets)."""
    name = exc.__class__.__name__
    message = str(exc).split("\n")[0][:300]
    hints = {
        "AuthenticationError": "Authentication failed: check the API key.",
        "NotFoundError": "Model or URL not found: check the model name and base URL.",
        "Timeout": "The model did not answer in time.",
        "APIConnectionError": "Could not connect: check the base URL and that the server is running "
        "(inside Docker use host.docker.internal instead of localhost).",
        "RateLimitError": "The provider rate limit was reached.",
        "BadRequestError": "The provider rejected the request.",
    }
    for key, hint in hints.items():
        if key in name:
            return f"{hint} ({message})"
    return f"{name}: {message}"
