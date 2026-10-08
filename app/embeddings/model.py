"""Embedding model management (sentence-transformers, default BAAI/bge-m3).

* :func:`download_model` runs in the worker as a job and reports byte-level progress.
* :func:`get_embedder` lazily loads the model once per process (app and worker),
  from the local cache only, so no network access is needed after the download.
* Tests replace the embedder with :func:`set_embedder_for_tests`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from app.config import get_settings

logger = logging.getLogger(__name__)

MAX_SEQ_LENGTH = 1024  # tokens; chunks are ~500 tokens so this bounds CPU cost
BATCH_SIZE = 16

# Files we never need for sentence-transformers inference (bge-m3 also ships ONNX
# exports and ColBERT/sparse heads that would double the download).
_IGNORE_PATTERNS = ["onnx/*", "*.onnx", "*.onnx_data", "openvino/*", "*.pt", "*.h5", "*.msgpack", "*.ot", "imgs/*", "*.png", "*.jpg"]


class Embedder(Protocol):
    """What the indexer and search need from an embedding model."""

    name: str
    dim: int

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...

    def count_tokens(self, text: str) -> int: ...


def cache_dir() -> Path:
    return Path(os.environ.get("HF_HOME") or (get_settings().data_dir / "models"))


# ----------------------------------------------------------------------------- download
def download_model(
    name: str,
    progress: Callable[[float, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    """Download ``name`` from Hugging Face into the shared cache; returns the local path."""
    from huggingface_hub import HfApi, snapshot_download

    api = HfApi()
    info = api.model_info(name, files_metadata=True)
    siblings = [s for s in (info.siblings or [])]
    names = {s.rfilename for s in siblings}
    ignore = list(_IGNORE_PATTERNS)
    if any(n.endswith(".safetensors") for n in names):
        ignore.append("*.bin")  # prefer safetensors weights when both exist
    from fnmatch import fnmatch

    wanted = [s for s in siblings if not any(fnmatch(s.rfilename, pat) for pat in ignore)]
    total = sum((s.size or 0) for s in wanted) or 1

    target = cache_dir() / ("models--" + name.replace("/", "--"))
    result: dict[str, Any] = {}

    def _run() -> None:
        try:
            result["path"] = snapshot_download(name, ignore_patterns=ignore, cache_dir=str(cache_dir() / "hub"))
        except Exception as exc:  # noqa: BLE001 - reported to the job
            result["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    hub_dir = cache_dir() / "hub" / target.name
    while thread.is_alive():
        if cancelled and cancelled():
            raise InterruptedError("Download cancelled")
        done = _dir_size(hub_dir)
        if progress:
            progress(min(99.0, done * 100 / total), f"{done / 1e6:,.0f} of {total / 1e6:,.0f} MB")
        time.sleep(1.0)
    if "error" in result:
        raise result["error"]
    if progress:
        progress(100.0, f"{total / 1e6:,.0f} MB downloaded")
    return str(result["path"])


def _dir_size(path: Path) -> int:
    size = 0
    if path.exists():
        for root, _dirs, files in os.walk(path):
            for f in files:
                fp = Path(root) / f
                if not fp.is_symlink():
                    try:
                        size += fp.stat().st_size
                    except OSError:
                        pass
    return size


# ----------------------------------------------------------------------------- loading
class SentenceTransformerEmbedder:
    """Embedder backed by a locally cached sentence-transformers model."""

    def __init__(self, name: str, path: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.name = name
        self._model = SentenceTransformer(path, device="cpu", local_files_only=True)
        self._model.max_seq_length = MAX_SEQ_LENGTH
        self.dim = int(self._model.get_sentence_embedding_dimension() or 0)
        self._lock = threading.Lock()  # torch inference is not re-entrant-safe across threads

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            vectors = self._model.encode(
                list(texts), batch_size=BATCH_SIZE, normalize_embeddings=True, show_progress_bar=False
            )
        return [v.tolist() for v in vectors]

    def count_tokens(self, text: str) -> int:
        return len(self._model.tokenizer(text, add_special_tokens=False)["input_ids"])


_embedder: Embedder | None = None
_embedder_lock = threading.Lock()


class EmbeddingNotReady(RuntimeError):
    """The embedding model has not been downloaded yet (see Settings → Embeddings)."""


def get_embedder(config: dict[str, Any] | None = None) -> Embedder:
    """Return the process-wide embedder, loading it on first use.

    ``config`` is the ``embedding`` setting; when omitted it is read from the database.
    """
    global _embedder
    if config is None:
        from app.db.session import session_scope
        from app.services.settings import get_setting

        with session_scope() as db:
            config = get_setting(db, "embedding")
    if _embedder is not None and _embedder.name == config.get("model"):
        return _embedder
    if config.get("status") != "ready" or not config.get("path"):
        raise EmbeddingNotReady("The embedding model is not downloaded yet.")
    with _embedder_lock:
        if _embedder is None or _embedder.name != config["model"]:
            logger.info("Loading embedding model %s", config["model"])
            _embedder = SentenceTransformerEmbedder(config["model"], config["path"])
            logger.info("Embedding model loaded (dim=%s)", _embedder.dim)
    return _embedder


def set_embedder_for_tests(embedder: Embedder | None) -> None:
    global _embedder
    _embedder = embedder


def load_for_download_check(name: str, path: str) -> int:
    """Load the freshly downloaded model to verify it works; return its dimension."""
    global _embedder
    with _embedder_lock:
        _embedder = SentenceTransformerEmbedder(name, path)
        return _embedder.dim
