"""Choosing the embedding model (local or cloud), measured speed, and indexing-time estimates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AIModel, Client
from app.embeddings.catalog import API_PROVIDERS, OPTIONS, EmbeddingOption, find_option
from app.indexer.schema import ensure_embedding_column
from app.security.crypto import decrypt_secret, encrypt_secret
from app.services import jobs as job_service
from app.services.settings import get_setting, get_settings_map, set_setting, update_setting_dict

CHUNKS_PER_PAGE = 1.2  # a dense page is ~600 tokens; chunks hold ~500
TOKENS_PER_CHUNK = 500
OCR_PAGES_PER_S = 2.0  # rough guess for scanned PDFs on a typical server CPU
LARGE_DOCUMENT_SECONDS = 10 * 60  # ask before a local model spends longer than this on one upload


class EmbeddingChoiceError(ValueError):
    """Invalid embedding choice; the message is safe to show in the UI."""


def model_identity(config: dict[str, Any]) -> str | None:
    """Stable name of the configured model (cloud models are prefixed with their provider)."""
    from app.embeddings.model import embedder_name

    return embedder_name(config)


# ----------------------------------------------------------------------------- speed
def record_rate(db: Session, name: str, device: str, chunks: int, seconds: float) -> None:
    """Remember measured embedding speed (chunks/s) per model and device; smoothed across runs."""
    if chunks < 8 or seconds <= 0:
        return
    rates = dict(get_setting(db, "embedding_rate") or {})
    per_model = dict(rates.get(name) or {})
    measured = chunks / seconds
    previous = per_model.get(device)
    per_model[device] = round(measured if previous is None else 0.5 * previous + 0.5 * measured, 3)
    rates[name] = per_model
    set_setting(db, "embedding_rate", rates)


def _device(db: Session) -> str:
    """Where a local model runs on the worker: "cuda" or "cpu"."""
    heartbeat = get_setting(db, "worker_heartbeat") or {}
    if heartbeat.get("device") in ("cpu", "cuda"):
        return heartbeat["device"]
    return "cuda" if get_setting(db, "compute_device") == "gpu" and heartbeat.get("gpu") else "cpu"


def chunks_per_second(db: Session, option: EmbeddingOption | None, name: str | None, backend: str) -> tuple[float, bool]:
    """(speed, measured?) for a model: the measured speed on this server, else the catalog guess."""
    if backend == "api":
        measured = (get_setting(db, "embedding_rate") or {}).get(name or "", {}).get("api")
        return (measured, True) if measured else ((option.api_chunks_per_s if option else None) or 20.0, False)
    device = _device(db)
    measured = (get_setting(db, "embedding_rate") or {}).get(name or "", {}).get(device)
    if measured:
        return measured, True
    guess = (option.gpu_chunks_per_s if device == "cuda" else option.cpu_chunks_per_s) if option else None
    return guess or (20.0 if device == "cuda" else 0.5), False


# ----------------------------------------------------------------------------- estimates
def estimate_pages(path: Path) -> tuple[int, bool]:
    """(pages, scanned?) quickly, without reading the whole document."""
    if path.suffix.lower() == ".pdf":
        try:
            import pypdfium2 as pdfium

            pdf = pdfium.PdfDocument(str(path))
            try:
                total = len(pdf)
                sample = range(0, total, max(1, total // 5))[:5]  # a few pages spread through the file
                empty = 0
                for i in sample:
                    page = pdf[i]
                    textpage = page.get_textpage()
                    if len(textpage.get_text_bounded().strip()) < 50:
                        empty += 1
                    textpage.close()
                    page.close()
                return total, bool(sample) and empty * 2 > len(sample)
            finally:
                pdf.close()
        except Exception:  # noqa: BLE001 - unreadable files are reported by the indexer
            pass
    # Office and text files: about 3 KB of text per page.
    try:
        return max(1, path.stat().st_size // 3000), False
    except OSError:
        return 1, False


def _duration(pages: int, scanned: bool, rate: float) -> float:
    seconds = pages * CHUNKS_PER_PAGE / rate
    if scanned:
        seconds += pages / OCR_PAGES_PER_S
    return seconds


def estimate_indexing(db: Session, pages: int, scanned: bool = False) -> dict[str, Any]:
    """Estimated time with the current model and with each cloud model, plus cloud cost."""
    config = get_setting(db, "embedding") or {}
    backend = config.get("backend") or "local"
    name = model_identity(config)
    current = find_option(backend, config.get("provider") or "huggingface", config.get("model") or "")
    rate, measured = chunks_per_second(db, current, name, backend)
    tokens = pages * CHUNKS_PER_PAGE * TOKENS_PER_CHUNK
    cloud = []
    for option in OPTIONS:
        if option.backend != "api":
            continue
        option_rate, _ = chunks_per_second(db, option, f"{option.provider}/{option.model}", "api")
        cloud.append({
            "key": option.key,
            "label": option.label,
            "provider": option.provider,
            "model": option.model,
            "seconds": round(_duration(pages, scanned, option_rate)),
            "usd": round(tokens / 1e6 * option.usd_per_m_tokens, 4) if option.usd_per_m_tokens is not None else None,
            "recommended": option.recommended,
        })
    return {
        "pages": pages,
        "scanned": scanned,
        "chunks": round(pages * CHUNKS_PER_PAGE),
        "current": {
            "backend": backend,
            "label": current.label if current else name,
            "model": name,
            "device": "api" if backend == "api" else _device(db),
            "seconds": round(_duration(pages, scanned, rate)),
            "measured": measured,
        },
        "cloud": cloud,
    }


def large_upload_warning(db: Session, paths: list[Path]) -> dict[str, Any] | None:
    """An estimate to show before indexing, when a local model would take too long; else None."""
    config = get_setting(db, "embedding") or {}
    if (config.get("backend") or "local") == "api" or not paths:
        return None
    pages = 0
    scanned_pages = 0
    files = []
    for path in paths:
        n, scanned = estimate_pages(path)
        pages += n
        scanned_pages += n if scanned else 0
        files.append({"filename": path.name, "pages": n, "scanned": scanned})
    estimate = estimate_indexing(db, pages, scanned=scanned_pages * 2 > pages)
    if estimate["current"]["seconds"] < LARGE_DOCUMENT_SECONDS:
        return None
    return {**estimate, "files": files}


# ----------------------------------------------------------------------------- options for the UI
def options_view(db: Session) -> list[dict[str, Any]]:
    """Every catalog model with its estimated time for 1,000 pages on this server."""
    saved_keys = {m.provider for m in db.scalars(select(AIModel)) if m.api_key_encrypted}
    out = []
    for option in OPTIONS:
        name = option.model if option.backend == "local" else f"{option.provider}/{option.model}"
        rate, measured = chunks_per_second(db, option, name, option.backend)
        out.append({
            **option.as_dict(),
            "provider_label": API_PROVIDERS.get(option.provider, "Hugging Face (local)"),
            "seconds_per_1000_pages": round(1000 * CHUNKS_PER_PAGE / rate),
            "measured": measured,
            "usd_per_1000_pages": round(1000 * CHUNKS_PER_PAGE * TOKENS_PER_CHUNK / 1e6 * option.usd_per_m_tokens, 3)
            if option.usd_per_m_tokens is not None else None,
            "saved_key_available": option.provider in saved_keys,
        })
    return out


# ----------------------------------------------------------------------------- switching
def queue_reindex_all(db: Session) -> int:
    clients = list(db.scalars(select(Client)))
    for client in clients:
        job_service.enqueue(db, "reindex", client.id, triggered_by="system")
    return len(clients)


def _saved_key(db: Session, provider: str) -> str | None:
    for model in db.scalars(select(AIModel).where(AIModel.provider == provider)):
        if model.api_key_encrypted:
            key = decrypt_secret(model.api_key_encrypted)
            if key:
                return key
    return None


def switch_to_api(db: Session, provider: str, model: str, api_key: str | None, dim: int | None = None) -> dict[str, Any]:
    """Verify a cloud embedding model with one call, make it current and re-index if it changed. Commits."""
    from app.embeddings.api import ApiEmbedder, EmbeddingAPIError

    if provider not in API_PROVIDERS:
        raise EmbeddingChoiceError("Choose OpenAI, Google Gemini or Mistral for cloud embeddings.")
    model = (model or "").strip()
    if not model:
        raise EmbeddingChoiceError("Enter the embedding model name.")
    option = find_option("api", provider, model)
    default_dim = option.dim if option else (1024 if provider == "mistral" else 1536)
    dim = int(dim or default_dim)
    current = get_setting(db, "embedding") or {}
    key = (api_key or "").strip() or None
    if key is None and current.get("backend") == "api" and current.get("provider") == provider and current.get("api_key_encrypted"):
        key = decrypt_secret(current["api_key_encrypted"])
    key = key or _saved_key(db, provider)
    if not key:
        raise EmbeddingChoiceError(f"Enter an API key for {API_PROVIDERS[provider]}.")
    try:
        vector = ApiEmbedder(provider, model, key, dim).encode(["Connection test"])[0]
    except EmbeddingAPIError as exc:
        raise EmbeddingChoiceError(str(exc)) from exc
    dim = len(vector)  # what the provider actually returns
    previous = model_identity(current) if current.get("status") == "ready" else None
    recreated = ensure_embedding_column(db, dim)
    new = {
        "backend": "api", "provider": provider, "model": model, "dim": dim, "path": None, "status": "ready",
        "error": None, "pending_model": None, "job_id": None, "api_key_encrypted": encrypt_secret(key),
    }
    update_setting_dict(db, "embedding", new)
    queued = queue_reindex_all(db) if recreated or previous != f"{provider}/{model}" else 0
    db.commit()
    return {"reindex_clients": queued}


def embedding_view(db: Session) -> dict[str, Any]:
    config = get_settings_map(db, ["embedding"])["embedding"] or {}
    return {
        **{k: config.get(k) for k in ("model", "dim", "status", "error", "pending_model")},
        "backend": config.get("backend") or "local",
        "provider": config.get("provider") or "huggingface",
        "has_api_key": bool(config.get("api_key_encrypted")),
    }
