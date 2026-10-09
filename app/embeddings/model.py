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
GPU_BATCH_SIZE = 64

# Files we never need for sentence-transformers inference (bge-m3 also ships ONNX
# exports and ColBERT/sparse heads that would double the download).
_IGNORE_PATTERNS = ["onnx/*", "*.onnx", "*.onnx_data", "openvino/*", "*.pt", "*.h5", "*.msgpack", "*.ot", "imgs/*", "*.png", "*.jpg"]


class Embedder(Protocol):
    """What the indexer and search need from an embedding model."""

    name: str
    dim: int

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...

    def count_tokens(self, text: str) -> int: ...


def encode_queries(embedder: Embedder, texts: Sequence[str]) -> list[list[float]]:
    """Embed search questions; some models embed questions differently from passages."""
    method = getattr(embedder, "encode_queries", None)
    return method(texts) if method is not None else embedder.encode(texts)


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

    def __init__(self, name: str, path: str, device: str = "cpu") -> None:
        from sentence_transformers import SentenceTransformer

        from app.embeddings.catalog import find_local

        self.name = name
        self.device = device  # "cpu" | "cuda"
        option = find_local(name)
        self._query_prefix = option.query_prefix if option else ""
        self._passage_prefix = option.passage_prefix if option else ""
        self._model = SentenceTransformer(path, device=device, local_files_only=True)
        if device == "cuda":
            self._model.half()  # fp16: about twice as fast on GPUs, vectors are practically identical
        # Never above the model's own limit (e.g. 512 for small models).
        self._model.max_seq_length = min(MAX_SEQ_LENGTH, self._model.max_seq_length or MAX_SEQ_LENGTH)
        self.dim = int(self._model.get_sentence_embedding_dimension() or 0)
        self._batch_size = GPU_BATCH_SIZE if device == "cuda" else BATCH_SIZE
        self._lock = threading.Lock()  # torch inference is not re-entrant-safe across threads

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode([self._passage_prefix + t for t in texts])

    def encode_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode([self._query_prefix + t for t in texts])

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            vectors = self._model.encode(texts, batch_size=self._batch_size, normalize_embeddings=True, show_progress_bar=False)
        return [v.astype("float32").tolist() for v in vectors]

    def count_tokens(self, text: str) -> int:
        return len(self._model.tokenizer(text, add_special_tokens=False)["input_ids"])


# ----------------------------------------------------------------------------- device
COMPUTE_DEVICES = ("cpu", "gpu")  # values of the ``compute_device`` setting
_gpu_info: dict[str, Any] | None | bool = False  # False = not probed yet
_gpu_warned = False


def gpu_info() -> dict[str, Any] | None:
    """Describe the NVIDIA GPU visible to this process, or None (no GPU, or a CPU-only torch build)."""
    global _gpu_info
    if _gpu_info is False:
        _gpu_info = None
        try:
            import torch

            if torch.cuda.is_available():
                props = torch.cuda.get_device_properties(0)
                _gpu_info = {"name": props.name, "memory_gb": round(props.total_memory / 1e9, 1), "cuda": torch.version.cuda}
        except Exception:  # noqa: BLE001 - a broken driver must not break the app
            logger.warning("GPU detection failed", exc_info=True)
    return _gpu_info  # type: ignore[return-value]


def resolve_device(setting: str | None) -> str:
    """Map the ``compute_device`` setting to a torch device, falling back to CPU without a usable GPU."""
    global _gpu_warned
    if setting == "gpu":
        if gpu_info():
            return "cuda"
        if not _gpu_warned:
            logger.warning("Compute device is set to GPU, but this process sees no GPU: using the CPU")
            _gpu_warned = True
    return "cpu"


def _load(name: str, path: str, device: str) -> SentenceTransformerEmbedder:
    """Load on ``device``; if the GPU fails (e.g. out of memory), fall back to the CPU."""
    logger.info("Loading embedding model %s on %s", name, device)
    try:
        embedder = SentenceTransformerEmbedder(name, path, device)
    except RuntimeError:
        if device == "cpu":
            raise
        logger.exception("Could not load the embedding model on the GPU: using the CPU")
        embedder = SentenceTransformerEmbedder(name, path, "cpu")
    logger.info("Embedding model loaded (dim=%s, device=%s)", embedder.dim, embedder.device)
    return embedder


def _unload() -> None:
    """Drop the loaded model (caller holds the lock) and free its GPU memory before loading another."""
    global _embedder
    on_gpu = getattr(_embedder, "device", None) == "cuda"
    _embedder = None
    if on_gpu:
        import gc

        import torch

        gc.collect()
        torch.cuda.empty_cache()


def current_device() -> str | None:
    """Device of the embedder loaded in this process ("cpu" / "cuda"), or None if none is loaded."""
    return getattr(_embedder, "device", None)


_embedder: Embedder | None = None
_requested_device: str | None = None  # device asked for when _embedder was loaded
_embedder_lock = threading.Lock()


class EmbeddingNotReady(RuntimeError):
    """The embedding model has not been downloaded yet (see Settings → Embeddings)."""


def embedder_name(config: dict[str, Any]) -> str | None:
    """Name the embedder for ``config`` will have (cloud models are prefixed with their provider)."""
    if config.get("backend") == "api":
        return f"{config.get('provider')}/{config.get('model')}"
    return config.get("model")


def _matches(name: str | None, device: str) -> bool:
    """Is the loaded embedder this model, loaded for this device request?"""
    if _embedder is None or _embedder.name != name:
        return False
    # Test doubles have no device and match any request. A GPU request that fell back
    # to the CPU still counts as served, so a failing GPU isn't retried on every call.
    return not hasattr(_embedder, "device") or _requested_device == device


def get_embedder(config: dict[str, Any] | None = None, device_setting: str | None = None) -> Embedder:
    """Return the process-wide embedder, loading it on first use.

    ``config`` is the ``embedding`` setting and ``device_setting`` the ``compute_device``
    setting; whichever is omitted is read from the database. Changing either (in
    Settings) makes the next call load the model again, so no restart is needed.
    """
    global _embedder, _requested_device
    if config is None or device_setting is None:
        from app.db.session import session_scope
        from app.services.settings import get_settings_map

        with session_scope() as db:
            values = get_settings_map(db, ["embedding", "compute_device"])
        config = config if config is not None else values["embedding"]
        device_setting = device_setting if device_setting is not None else values["compute_device"]
    if config.get("backend") == "api":
        return _api_embedder(config)
    device = resolve_device(device_setting)
    if _matches(config.get("model"), device):
        return _embedder  # type: ignore[return-value]
    if config.get("status") != "ready" or not config.get("path"):
        raise EmbeddingNotReady("The embedding model is not downloaded yet.")
    with _embedder_lock:
        if not _matches(config["model"], device):
            _unload()
            _embedder = _load(config["model"], config["path"], device)
            _requested_device = device
    return _embedder  # type: ignore[return-value]


def _api_embedder(config: dict[str, Any]) -> Embedder:
    """The cloud embedder for ``config``, reused while provider, model, size and key stay the same."""
    global _embedder, _requested_device
    from app.embeddings.api import ApiEmbedder
    from app.security.crypto import decrypt_secret

    # "downloading": a local model is being fetched to replace this one; keep serving until it's ready.
    if config.get("status") not in ("ready", "downloading"):
        raise EmbeddingNotReady("The embedding model is not set up yet.")
    api_key = decrypt_secret(config["api_key_encrypted"]) if config.get("api_key_encrypted") else None
    if not api_key:
        raise EmbeddingNotReady("The API key of the cloud embedding model is missing or unreadable: enter it again in Settings → Embeddings.")
    with _embedder_lock:
        current = _embedder
        if not (
            isinstance(current, ApiEmbedder)
            and current.name == embedder_name(config)
            and current.dim == config.get("dim")
            and current.api_key == api_key
        ):
            _unload()
            _embedder = ApiEmbedder(config["provider"], config["model"], api_key, int(config["dim"]))
            _requested_device = "api"
        return _embedder  # type: ignore[return-value]


def preload_embedder() -> None:
    """Load the model in the background so the first visitor question doesn't wait for it."""

    def _run() -> None:
        try:
            get_embedder()
        except EmbeddingNotReady:
            logger.info("Embedding model not downloaded yet; nothing to preload")
        except Exception:  # noqa: BLE001 - a failed preload just means loading on first use
            logger.warning("Could not preload the embedding model", exc_info=True)

    threading.Thread(target=_run, name="embedder-preload", daemon=True).start()


def set_embedder_for_tests(embedder: Embedder | None) -> None:
    global _embedder, _requested_device
    _embedder, _requested_device = embedder, None


def load_for_download_check(name: str, path: str, device_setting: str = "cpu") -> int:
    """Load the freshly downloaded model to verify it works; return its dimension."""
    global _embedder, _requested_device
    device = resolve_device(device_setting)
    with _embedder_lock:
        _unload()
        _embedder = _load(name, path, device)
        _requested_device = device
        return _embedder.dim
