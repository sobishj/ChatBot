"""Thin wrapper around ``litellm.completion`` used for every model call.

All configuration comes from an :class:`~app.db.models.AIModel` row, so adding or
switching models never needs a restart or code change.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
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
    kwargs.update(_fast_thinking(cfg))
    kwargs.update(_claude_options(cfg))
    return kwargs


_CLAUDE_VERSION_RE = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?!\d)")


def _claude_options(cfg: ModelConfig) -> dict[str, Any]:
    """Settings for current Claude models (sampling and effort).

    Claude Opus 4.7+, Sonnet 5+, Haiku 5+ and Fable/Mythos reject non-default sampling
    parameters, so ``temperature`` is left out (:func:`complete` drops it when this
    returns ``_no_sampling``). Chat answers are short and grounded in the retrieved
    context, so low effort gives faster replies without worse answers.
    """
    if cfg.provider != "anthropic":
        return {}
    match = _CLAUDE_VERSION_RE.search(cfg.model_name.lower())
    if not match:
        return {}
    family, major, minor = match.group(1), int(match.group(2)), int(match.group(3) or 0)
    version = major + minor / 10
    no_sampling = (
        family in ("fable", "mythos")
        or (family == "opus" and version >= 4.7)
        or (family in ("sonnet", "haiku") and version >= 5)
    )
    effort = no_sampling or (family in ("opus", "sonnet") and version >= 4.6)
    options: dict[str, Any] = {}
    if no_sampling:
        options["_no_sampling"] = True
    if effort:
        options["output_config"] = {"effort": "low"}
    return options


_GEMINI_VERSION_RE = re.compile(r"gemini-(\d+(?:\.\d+)?)")


def _fast_thinking(cfg: ModelConfig) -> dict[str, Any]:
    """Keep Gemini's hidden "thinking" to a minimum.

    Answers are short and grounded in the retrieved context, so deep reasoning adds
    latency (about 1 s per answer) and cost without better answers. Thinking tokens
    also count against max_tokens, which can cut answers short.
    """
    if cfg.provider != "gemini":
        return {}
    match = _GEMINI_VERSION_RE.search(cfg.model_name.lower())
    if not match:
        return {}
    version = float(match.group(1))
    flash = "flash" in cfg.model_name.lower()
    if version >= 3:
        # Gemini 3+ can't switch thinking off; Flash models support "minimal", Pro models "low".
        return {"thinkingConfig": {"thinkingLevel": "minimal" if flash else "low"}}
    if version >= 2.5 and flash:
        return {"thinkingConfig": {"thinkingBudget": 0}}  # 2.5 Flash can switch thinking off
    return {}


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

    kwargs = _litellm_kwargs(cfg)
    if not kwargs.pop("_no_sampling", False):
        kwargs["temperature"] = cfg.temperature
    started = time.perf_counter()
    try:
        response = litellm.completion(
            messages=messages,
            max_tokens=max_tokens or cfg.max_tokens,
            timeout=cfg.timeout_seconds,
            num_retries=0,
            **kwargs,
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


# ----------------------------------------------------------------------------- tool calling
class ToolsUnsupported(LLMError):
    """The provider or model rejected tool definitions; callers fall back to a plain answer."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str


@dataclass
class ToolResult(LLMResult):
    tool_calls: list[ToolCall] = field(default_factory=list)


def _parse_tool_calls(message: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for i, call in enumerate(getattr(message, "tool_calls", None) or []):
        function = getattr(call, "function", None)
        name = getattr(function, "name", None)
        if not name:
            continue
        raw = getattr(function, "arguments", None) or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            args = {"_invalid_arguments": str(raw)[:200]}  # rejected by validation, reported to the model
        if not isinstance(args, dict):
            args = {"_invalid_arguments": str(raw)[:200]}
        calls.append(ToolCall(id=getattr(call, "id", None) or f"call_{i}", name=name, arguments=args, raw_arguments=raw if isinstance(raw, str) else json.dumps(raw)))
    return calls


def complete_with_tools(
    cfg: ModelConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, max_tokens: int | None = None
) -> ToolResult:
    """Like :func:`complete`, but the model may answer with tool calls instead of text.

    With no ``tools`` it is a plain completion over a conversation that may contain earlier
    tool calls and results. Raises :class:`ToolsUnsupported` when the model can't use tools.
    """
    import litellm

    litellm.telemetry = False
    litellm.suppress_debug_info = True

    kwargs = _litellm_kwargs(cfg)
    if not kwargs.pop("_no_sampling", False):
        kwargs["temperature"] = cfg.temperature
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    started = time.perf_counter()
    try:
        response = litellm.completion(
            messages=messages,
            max_tokens=max_tokens or cfg.max_tokens,
            timeout=cfg.timeout_seconds,
            num_retries=0,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - LiteLLM raises many exception types
        name = exc.__class__.__name__
        if tools and ("UnsupportedParams" in name or ("BadRequest" in name and re.search(r"\b(tool|function)", str(exc), re.I))):
            raise ToolsUnsupported(f"{cfg.display_name or cfg.model_name} does not support tool calling ({_friendly_error(exc)})") from exc
        raise LLMError(_friendly_error(exc)) from exc
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    try:
        message = response.choices[0].message
    except (AttributeError, IndexError) as exc:
        raise LLMError("The model returned an unexpected response.") from exc
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    return ToolResult(
        text=(message.content or "").strip() if isinstance(message.content, str) else "",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        response_ms=elapsed_ms,
        cost=estimate_cost(cfg, input_tokens, output_tokens, response),
        model_id=cfg.model_id,
        model_name=cfg.display_name or cfg.model_name,
        tool_calls=_parse_tool_calls(message) if tools else [],
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


_MODEL_LIST_TTL = 600  # seconds; model lists rarely change, and the form refetches on every key edit
_model_list_cache: dict[str, tuple[float, list[str]]] = {}
_http_client: Any = None


def _http() -> Any:
    """One shared client so repeated lookups reuse the open (keep-alive) connection."""
    global _http_client
    if _http_client is None:
        import httpx

        # Retries cover brief connection failures (DNS hiccups, a container network still starting up).
        _http_client = httpx.Client(transport=httpx.HTTPTransport(retries=2))
    return _http_client


def list_available_models(
    provider_key: str, base_url: str | None, api_key: str | None, timeout: float = 15, use_cache: bool = True
) -> list[str]:
    """Ask the provider which chat models this API key can use (each provider's ``/models`` endpoint)."""
    import hashlib

    provider = get_provider(provider_key)
    base = (base_url or provider.default_base_url or "").strip().rstrip("/")
    if not base:
        raise LLMError("Enter the base URL first.")
    if provider.needs_api_key and not api_key:
        raise LLMError("Enter the API key first.")
    cache_key = hashlib.sha256(f"{provider_key}\n{base}\n{api_key or ''}".encode()).hexdigest()
    cached = _model_list_cache.get(cache_key)
    if use_cache and cached and time.monotonic() - cached[0] < _MODEL_LIST_TTL:
        return cached[1]
    names = _fetch_model_list(provider_key, base, api_key, timeout)
    _model_list_cache[cache_key] = (time.monotonic(), names)
    return names


def _fetch_model_list(provider_key: str, base: str, api_key: str | None, timeout: float) -> list[str]:
    import httpx

    http = _http()
    try:
        if provider_key == "anthropic":
            url = base if base.endswith("/v1") else f"{base}/v1"
            resp = http.get(f"{url}/models", params={"limit": 1000}, timeout=timeout,
                            headers={"x-api-key": api_key or "", "anthropic-version": "2023-06-01"})
            resp.raise_for_status()
            names = [m["id"] for m in resp.json().get("data", [])]
        elif provider_key == "gemini":
            url = base if base.endswith(("/v1", "/v1beta")) else f"{base}/v1beta"
            resp = http.get(f"{url}/models", params={"pageSize": 1000}, timeout=timeout, headers={"x-goog-api-key": api_key or ""})
            resp.raise_for_status()
            names = [
                m["name"].removeprefix("models/")
                for m in resp.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
            ]
        else:  # OpenAI and OpenAI-compatible servers (incl. Ollama, vLLM, LM Studio)
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            resp = http.get(f"{base}/models", timeout=timeout, headers=headers)
            resp.raise_for_status()
            names = [m["id"] for m in resp.json().get("data", [])]
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            raise LLMError(f"Authentication failed: check the API key. (HTTP {code})") from exc
        if code == 404:
            raise LLMError("This server has no model list at that URL: check the base URL, or type the model name manually.") from exc
        raise LLMError(f"The provider returned HTTP {code}.") from exc
    except httpx.HTTPError as exc:
        raise LLMError(
            "Could not connect: check the base URL and that the server is running "
            f"(inside Docker use host.docker.internal instead of localhost). ({exc.__class__.__name__}: {str(exc)[:200]})"
        ) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise LLMError("The provider returned an unexpected model list.") from exc
    return sorted(set(names))


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
