"""Skip models that are failing right now, so visitors don't wait for the same failure again.

A model that fails with an outage-type error (busy, rate-limited, timed out, unreachable,
rejected key) is set aside for a cooldown that doubles on repeated failures (2 min up to
10 min). Chat tries healthy models first; models in cooldown are still tried last, so
nothing is lost when every model is having trouble. A success clears the cooldown.

State is per process (the app serves chat from one process); it resets on restart.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.llm.client import ModelConfig

COOLDOWN_SECONDS = 120
MAX_COOLDOWN_SECONDS = 600

# Failures that say "this model can't answer anyone right now", not "this request was bad".
_OUTAGE_NAMES = ("RateLimit", "ServiceUnavailable", "Timeout", "APIConnection", "InternalServer", "Authentication", "PermissionDenied", "NotFound")

_lock = threading.Lock()
_state: dict[str, tuple[float, float]] = {}  # model key -> (cooling until, last cooldown length)


def _key(cfg: ModelConfig) -> str:
    return f"{cfg.model_id}:{cfg.provider}:{cfg.model_name}:{cfg.base_url or ''}"


def is_outage(exc: BaseException) -> bool:
    """True for errors that will hit every visitor, not just this question."""
    seen: BaseException | None = exc
    while seen is not None:
        name = seen.__class__.__name__
        if any(part in name for part in _OUTAGE_NAMES) or name in ("ConnectError", "ReadTimeout", "ConnectTimeout"):
            return True
        seen = seen.__cause__
    return False


def cooling(cfg: ModelConfig) -> bool:
    with _lock:
        until, _ = _state.get(_key(cfg), (0.0, 0.0))
    return until > time.monotonic()


def record_failure(cfg: ModelConfig, exc: BaseException) -> None:
    if not is_outage(exc):
        return
    with _lock:
        _, last = _state.get(_key(cfg), (0.0, 0.0))
        length = min(MAX_COOLDOWN_SECONDS, last * 2 if last else COOLDOWN_SECONDS)
        _state[_key(cfg)] = (time.monotonic() + length, length)


def record_success(cfg: ModelConfig) -> None:
    with _lock:
        _state.pop(_key(cfg), None)


def healthy_first(chain: list[ModelConfig]) -> list[ModelConfig]:
    """The same models, those in cooldown moved to the end (order otherwise kept)."""
    return [c for c in chain if not cooling(c)] + [c for c in chain if cooling(c)]


def reset() -> None:
    with _lock:
        _state.clear()
