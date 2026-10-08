"""Test doubles: a deterministic embedder and a scriptable LLM."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.llm.client import LLMError, LLMResult, ModelConfig
from app.search.hybrid import words

DIM = 64


class FakeEmbedder:
    """Bag-of-words hashing embedder: texts sharing words get similar vectors."""

    name = "fake-embedder"
    dim = DIM

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * DIM
            for w in words(text):
                h = int(hashlib.md5(w.encode()).hexdigest(), 16)
                vec[h % DIM] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    def count_tokens(self, text: str) -> int:
        return len(text.split())


@dataclass
class FakeLLM:
    """Callable replacing ``app.llm.client.complete``. Records every call."""

    reply: str = "Fake answer."
    fail_models: set[str] = field(default_factory=set)
    calls: list[tuple[str, list[dict[str, str]]]] = field(default_factory=list)

    def __call__(self, cfg: ModelConfig, messages: list[dict[str, str]], max_tokens: int | None = None) -> LLMResult:
        self.calls.append((cfg.model_name, messages))
        if cfg.model_name in self.fail_models:
            raise LLMError(f"{cfg.model_name} is unreachable")
        return LLMResult(
            text=self.reply, input_tokens=100, output_tokens=20, response_ms=5, cost=0.001,
            model_id=cfg.model_id, model_name=cfg.display_name or cfg.model_name,
        )
