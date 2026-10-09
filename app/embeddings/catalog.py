"""Embedding models offered in Settings → Embeddings, with what admins need to choose between them.

Local models run on this server (data stays here; speed depends on the CPU/GPU). Cloud
models run at the provider: indexing is far faster, but document text is sent to them.
Speeds and prices are rough guides for the UI, not guarantees; measured local speed
replaces the guess once something has been indexed (see ``app.services.embeddings``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class EmbeddingOption:
    backend: str  # "local" (sentence-transformers) | "api" (provider HTTP API)
    provider: str  # "huggingface" | "openai" | "gemini" | "mistral"
    model: str
    label: str
    dim: int
    languages: str
    summary: str
    # Rough throughput in chunks/second, used until real indexing has been measured.
    cpu_chunks_per_s: float | None = None
    gpu_chunks_per_s: float | None = None
    api_chunks_per_s: float | None = None
    usd_per_m_tokens: float | None = None  # list price, cloud only
    # Some models embed questions and passages differently.
    query_prefix: str = ""
    passage_prefix: str = ""
    recommended: bool = False

    @property
    def key(self) -> str:
        return option_key(self.backend, self.provider, self.model)

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "key": self.key}


def option_key(backend: str, provider: str, model: str) -> str:
    return f"{backend}:{provider}:{model}"


OPTIONS: list[EmbeddingOption] = [
    # ------------------------------------------------------------------ local
    EmbeddingOption(
        "local", "huggingface", "BAAI/bge-m3", "BGE-M3", 1024,
        "100+ languages incl. Malayalam, Hindi, Arabic",
        "Best search quality of the local models. Slow on a CPU: large documents need a GPU or a lot of patience.",
        cpu_chunks_per_s=0.5, gpu_chunks_per_s=40, recommended=True,
    ),
    EmbeddingOption(
        "local", "huggingface", "intfloat/multilingual-e5-small", "Multilingual E5 small", 384,
        "~100 languages incl. Malayalam, Hindi, Arabic",
        "About 10x faster than BGE-M3 on a CPU, with somewhat weaker search. A good local choice for large document sets.",
        cpu_chunks_per_s=5, gpu_chunks_per_s=200, query_prefix="query: ", passage_prefix="passage: ",
    ),
    EmbeddingOption(
        "local", "huggingface", "BAAI/bge-small-en-v1.5", "BGE small (English)", 384,
        "English only",
        "Fast on a CPU and good for English-only content. Do not use it for other languages.",
        cpu_chunks_per_s=6, gpu_chunks_per_s=250,
    ),
    # ------------------------------------------------------------------ cloud
    EmbeddingOption(
        "api", "openai", "text-embedding-3-small", "OpenAI text-embedding-3-small", 1536,
        "Multilingual",
        "Very fast and very cheap: thousands of pages in a minute or two. Document text is sent to OpenAI.",
        api_chunks_per_s=60, usd_per_m_tokens=0.02, recommended=True,
    ),
    EmbeddingOption(
        "api", "openai", "text-embedding-3-large", "OpenAI text-embedding-3-large", 1536,
        "Multilingual",
        "OpenAI's most accurate embedding model (shortened to 1536 dimensions to keep search indexed). Document text is sent to OpenAI.",
        api_chunks_per_s=50, usd_per_m_tokens=0.13,
    ),
    EmbeddingOption(
        "api", "gemini", "gemini-embedding-001", "Google gemini-embedding-001", 1536,
        "100+ languages incl. Malayalam, Hindi, Arabic",
        "Strong multilingual quality; has a free tier with rate limits. Document text is sent to Google.",
        api_chunks_per_s=30, usd_per_m_tokens=0.15,
    ),
    EmbeddingOption(
        "api", "mistral", "mistral-embed", "Mistral mistral-embed", 1024,
        "Mainly European languages",
        "Fast and inexpensive, best for English and European languages. Document text is sent to Mistral.",
        api_chunks_per_s=30, usd_per_m_tokens=0.10,
    ),
]

API_PROVIDERS = {"openai": "OpenAI", "gemini": "Google Gemini", "mistral": "Mistral"}


def find_option(backend: str, provider: str, model: str) -> EmbeddingOption | None:
    key = option_key(backend, provider, model)
    return next((o for o in OPTIONS if o.key == key), None)


def find_local(model: str) -> EmbeddingOption | None:
    return find_option("local", "huggingface", model)
