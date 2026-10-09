"""Test doubles: a deterministic embedder and scriptable LLMs (plain and tool-calling)."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.llm.client import LLMError, LLMResult, ModelConfig, ToolCall, ToolResult, ToolsUnsupported
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


@dataclass
class FakeToolLLM:
    """Callable replacing ``app.llm.client.complete_with_tools``.

    ``script`` holds one step per call: a string is a final text answer, a list of
    ``(action_name, arguments)`` is a round of tool calls. When the script runs out the
    model answers "Done." ``unsupported`` simulates a model that rejects tools.
    """

    script: list[Any] = field(default_factory=list)
    unsupported: bool = False
    fail_models: set[str] = field(default_factory=set)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, cfg: ModelConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, max_tokens: int | None = None) -> ToolResult:
        self.calls.append({"model": cfg.model_name, "messages": json.loads(json.dumps(messages, default=str)), "tools": tools})
        if cfg.model_name in self.fail_models:
            raise LLMError(f"{cfg.model_name} is unreachable")
        if self.unsupported and tools:
            raise ToolsUnsupported(f"{cfg.model_name} does not support tool calling")
        step = self.script.pop(0) if self.script else "Done."
        tool_calls: list[ToolCall] = []
        text = step if isinstance(step, str) else ""
        if not isinstance(step, str):
            if tools:
                tool_calls = [
                    ToolCall(id=f"call_{len(self.calls)}_{i}", name=name, arguments=args, raw_arguments=json.dumps(args))
                    for i, (name, args) in enumerate(step)
                ]
            else:
                text = "I couldn't finish that."  # no tools offered in the final round
        return ToolResult(
            text=text, input_tokens=50, output_tokens=10, response_ms=3, cost=0.0005,
            model_id=cfg.model_id, model_name=cfg.display_name or cfg.model_name, tool_calls=tool_calls,
        )
