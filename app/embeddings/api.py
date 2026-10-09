"""Embeddings from a provider's HTTP API (OpenAI, Google Gemini, Mistral).

Batches are sent several at a time, so thousands of chunks embed in a minute or two.
Rate limits (HTTP 429) and server errors are retried with backoff.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.llm.providers import get_provider

logger = logging.getLogger(__name__)

BATCH = {"openai": 128, "gemini": 100, "mistral": 16}  # inputs per request (provider limits)
CONCURRENCY = 6  # requests in flight at once
MAX_ATTEMPTS = 5
TIMEOUT = 60


class EmbeddingAPIError(RuntimeError):
    """A provider call failed; the message is safe to show in the admin UI."""


class ApiEmbedder:
    """Same interface as the local embedder, backed by a provider API."""

    device = "api"
    # The indexer hands over this many chunks at a time so requests can run in parallel.
    index_batch = 512

    def __init__(self, provider: str, model: str, api_key: str, dim: int, base_url: str | None = None) -> None:
        if provider not in BATCH:
            raise EmbeddingAPIError(f"Unsupported embedding provider: {provider}")
        self.provider = provider
        self.model = model
        self.name = f"{provider}/{model}"
        self.dim = dim
        self.api_key = api_key
        self._base = (base_url or get_provider(provider).default_base_url).rstrip("/")
        self._client: Any = None
        self._client_lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(list(texts), query=False)

    def encode_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(list(texts), query=True)

    def count_tokens(self, text: str) -> int:
        # Providers don't publish their tokenizers; cl100k (bundled with LiteLLM, works offline)
        # is close enough for sizing ~500-token chunks well below every provider's input limit.
        from litellm.litellm_core_utils.default_encoding import encoding

        return len(encoding.encode(text, disallowed_special=()))

    # ------------------------------------------------------------------ internals
    def _http(self) -> Any:
        with self._client_lock:
            if self._client is None:
                import httpx

                self._client = httpx.Client(timeout=TIMEOUT, transport=httpx.HTTPTransport(retries=2))
            return self._client

    def _encode(self, texts: list[str], query: bool) -> list[list[float]]:
        if not texts:
            return []
        size = BATCH[self.provider]
        batches = [texts[i : i + size] for i in range(0, len(texts), size)]
        if len(batches) == 1:
            results = [self._request(batches[0], query)]
        else:
            with ThreadPoolExecutor(max_workers=min(CONCURRENCY, len(batches)), thread_name_prefix="embed") as pool:
                results = list(pool.map(lambda b: self._request(b, query), batches))  # map keeps input order
        vectors = [v for batch in results for v in batch]
        if len(vectors) != len(texts):
            raise EmbeddingAPIError("The provider returned the wrong number of embeddings.")
        return [_normalize(v) for v in vectors]

    def _request(self, batch: list[str], query: bool) -> list[list[float]]:
        url, headers, body = self._build(batch, query)
        delay = 1.0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self._http().post(url, headers=headers, json=body)
            except Exception as exc:  # noqa: BLE001 - network errors are retried, then reported
                if attempt == MAX_ATTEMPTS:
                    raise EmbeddingAPIError(f"Could not reach the {self.provider} embedding API ({exc.__class__.__name__}).") from exc
            else:
                if resp.status_code == 200:
                    return self._parse(resp.json())
                if resp.status_code in (401, 403):
                    raise EmbeddingAPIError(f"The {self.provider} API rejected the API key (HTTP {resp.status_code}).")
                if resp.status_code not in (408, 429) and resp.status_code < 500:
                    raise EmbeddingAPIError(f"The {self.provider} embedding API returned HTTP {resp.status_code}: {_error_text(resp)}")
                if attempt == MAX_ATTEMPTS:
                    raise EmbeddingAPIError(
                        f"The {self.provider} embedding API kept failing (HTTP {resp.status_code}): {_error_text(resp)}"
                    )
                retry_after = resp.headers.get("retry-after")
                if retry_after and retry_after.replace(".", "", 1).isdigit():
                    delay = max(delay, min(float(retry_after), 60.0))
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
        raise EmbeddingAPIError("Embedding failed.")  # unreachable

    def _build(self, batch: list[str], query: bool) -> tuple[str, dict[str, str], dict[str, Any]]:
        if self.provider == "gemini":
            task = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
            requests = [
                {"model": f"models/{self.model}", "content": {"parts": [{"text": t}]}, "taskType": task, "outputDimensionality": self.dim}
                for t in batch
            ]
            base = self._base if self._base.endswith(("/v1", "/v1beta")) else f"{self._base}/v1beta"
            return f"{base}/models/{self.model}:batchEmbedContents", {"x-goog-api-key": self.api_key}, {"requests": requests}
        body: dict[str, Any] = {"model": self.model, "input": batch}
        if self.provider == "openai":
            body["encoding_format"] = "float"
            if self.model.startswith("text-embedding-3"):
                body["dimensions"] = self.dim
        return f"{self._base}/embeddings", {"Authorization": f"Bearer {self.api_key}"}, body

    def _parse(self, data: dict[str, Any]) -> list[list[float]]:
        try:
            if self.provider == "gemini":
                return [e["values"] for e in data["embeddings"]]
            return [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]
        except (KeyError, TypeError) as exc:
            raise EmbeddingAPIError("The provider returned an unexpected embedding response.") from exc


def _normalize(vector: list[float]) -> list[float]:
    """Unit length, like the local models (shortened Gemini vectors are not normalised by the API)."""
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def _error_text(resp: Any) -> str:
    try:
        data = resp.json()
        err = data.get("error", data)
        return str(err.get("message", err) if isinstance(err, dict) else err)[:300]
    except Exception:  # noqa: BLE001
        return (resp.text or "")[:300]
