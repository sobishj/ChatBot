"""Provider presets for the "Add model" form.

``litellm_prefix`` is how LiteLLM routes the call. Providers without a native LiteLLM
route (Moonshot, local servers) use the OpenAI-compatible route with an explicit base URL.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    litellm_prefix: str
    default_base_url: str
    cloud: bool  # sends data outside the server (warning in on-premise mode)
    needs_api_key: bool
    example_model: str
    # Native LiteLLM routes know their own endpoint; we only pass api_base when it was changed.
    openai_compatible: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


PROVIDERS: dict[str, Provider] = {
    p.key: p
    for p in [
        Provider("openai", "OpenAI (GPT)", "openai", "https://api.openai.com/v1", True, True, "gpt-4o-mini"),
        Provider("anthropic", "Anthropic (Claude)", "anthropic", "https://api.anthropic.com", True, True, "claude-sonnet-5-5"),
        Provider("gemini", "Google (Gemini)", "gemini", "https://generativelanguage.googleapis.com", True, True, "gemini-2.0-flash"),
        Provider("moonshot", "Moonshot (Kimi)", "openai", "https://api.moonshot.ai/v1", True, True, "kimi-k2-0905-preview", True),
        Provider("deepseek", "DeepSeek", "deepseek", "https://api.deepseek.com", True, True, "deepseek-chat"),
        Provider("mistral", "Mistral", "mistral", "https://api.mistral.ai/v1", True, True, "mistral-small-latest"),
        Provider("groq", "Groq", "groq", "https://api.groq.com/openai/v1", True, True, "llama-3.3-70b-versatile"),
        Provider("openrouter", "OpenRouter", "openrouter", "https://openrouter.ai/api/v1", True, True, "openai/gpt-4o-mini"),
        Provider("bionic", "Bionic (local, OpenAI-compatible)", "openai", "http://host.docker.internal:7800/v1", False, False, "qwen", True),
        Provider("ollama", "Ollama (local)", "openai", "http://host.docker.internal:11434/v1", False, False, "qwen2.5:7b", True),
        Provider("vllm", "vLLM (local)", "openai", "http://host.docker.internal:8000/v1", False, False, "Qwen/Qwen2.5-7B-Instruct", True),
        Provider("lmstudio", "LM Studio (local)", "openai", "http://host.docker.internal:1234/v1", False, False, "qwen2.5-7b-instruct", True),
        Provider("openai_compatible", "Other OpenAI-compatible server", "openai", "", False, False, "model-name", True),
    ]
}


def get_provider(key: str) -> Provider:
    try:
        return PROVIDERS[key]
    except KeyError as exc:
        raise ValueError(f"Unknown provider: {key}") from exc


def is_loopback_url(url: str | None) -> bool:
    """True for localhost/127.x URLs, which inside Docker point at the container itself."""
    if not url:
        return False
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host in {"localhost", "::1"} or host.startswith("127.")
