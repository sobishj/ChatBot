"""Cloud embeddings, model switching, measured speed and the large-upload warning."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job
from app.embeddings.api import ApiEmbedder, EmbeddingAPIError


class FakeProvider:
    """Replaces httpx.Client.post; answers like OpenAI or Gemini and records each request."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.statuses = list(statuses or [])

    def __call__(self, url: str, headers: dict[str, str] | None = None, json: dict[str, Any] | None = None) -> httpx.Response:
        self.requests.append({"url": url, "headers": headers, "json": json})
        status = self.statuses.pop(0) if self.statuses else 200
        request = httpx.Request("POST", url)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "slow down"}}, request=request)
        assert json is not None
        if "requests" in json:  # Gemini
            return httpx.Response(200, json={"embeddings": [{"values": [3.0, 4.0]} for _ in json["requests"]]}, request=request)
        # OpenAI-style, deliberately out of order: the embedder must sort by index.
        data = [{"index": i, "embedding": [float(len(t)), 0.0]} for i, t in enumerate(json["input"])]
        return httpx.Response(200, json={"data": list(reversed(data))}, request=request)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    fake = FakeProvider()
    monkeypatch.setattr(httpx.Client, "post", lambda _client, url, **kwargs: fake(url, **kwargs))
    monkeypatch.setattr("app.embeddings.api.time.sleep", lambda _s: None)
    return fake


def test_openai_batches_keep_order_and_are_normalised(provider: FakeProvider) -> None:
    texts = ["a" * (i % 7 + 1) for i in range(300)]
    vectors = ApiEmbedder("openai", "text-embedding-3-small", "sk-test", 1536).encode(texts)
    assert len(provider.requests) == 3  # 128 + 128 + 44
    assert provider.requests[0]["json"]["dimensions"] == 1536
    assert provider.requests[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert [v[0] for v in vectors] == [1.0] * 300  # [len, 0] normalised
    assert provider.requests[0]["url"] == "https://api.openai.com/v1/embeddings"


def test_gemini_uses_retrieval_task_types(provider: FakeProvider) -> None:
    embedder = ApiEmbedder("gemini", "gemini-embedding-001", "g-key", 1536)
    doc = embedder.encode(["passage"])[0]
    embedder.encode_queries(["question"])
    first, second = (r["json"]["requests"][0] for r in provider.requests)
    assert first["taskType"] == "RETRIEVAL_DOCUMENT" and second["taskType"] == "RETRIEVAL_QUERY"
    assert first["outputDimensionality"] == 1536
    assert provider.requests[0]["url"].endswith("/v1beta/models/gemini-embedding-001:batchEmbedContents")
    assert math.isclose(math.hypot(*doc), 1.0)


def test_rate_limits_are_retried_and_bad_keys_reported(provider: FakeProvider) -> None:
    provider.statuses = [429, 503]
    assert len(ApiEmbedder("mistral", "mistral-embed", "m-key", 1024).encode(["x"])) == 1
    assert len(provider.requests) == 3
    provider.statuses = [401]
    with pytest.raises(EmbeddingAPIError, match="API key"):
        ApiEmbedder("openai", "text-embedding-3-small", "bad", 1536).encode(["x"])


def test_switch_to_cloud_model_reindexes_and_serves_search(db: Session, embedder, make_client, provider: FakeProvider) -> None:
    from app.embeddings.model import get_embedder, set_embedder_for_tests
    from app.indexer.schema import current_embedding_dim
    from app.services.embeddings import EmbeddingChoiceError, embedding_view, switch_to_api
    from app.services.settings import get_setting

    make_client()
    set_embedder_for_tests(None)
    with pytest.raises(EmbeddingChoiceError, match="API key"):
        switch_to_api(db, "openai", "text-embedding-3-small", None)

    result = switch_to_api(db, "openai", "text-embedding-3-small", "sk-test")
    assert result["reindex_clients"] == 1
    assert db.scalars(select(Job).where(Job.type == "reindex")).first() is not None
    config = get_setting(db, "embedding")
    assert config["backend"] == "api" and config["dim"] == 2 and config["api_key_encrypted"] != "sk-test"
    assert current_embedding_dim(db) == 2
    view = embedding_view(db)
    assert view["has_api_key"] and "api_key_encrypted" not in view
    live = get_embedder()
    assert isinstance(live, ApiEmbedder) and live.name == "openai/text-embedding-3-small"
    # Blank key on a later switch reuses the stored one.
    switch_to_api(db, "openai", "text-embedding-3-small", "")
    assert provider.requests[-1]["headers"]["Authorization"] == "Bearer sk-test"
    set_embedder_for_tests(None)


def _blank_pdf(path: Path, pages: int) -> Path:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(595, 842)
    pdf.save(str(path))
    return path


def test_large_upload_warning_uses_measured_speed(db: Session, embedder, tmp_path: Path) -> None:
    from app.services.embeddings import estimate_pages, large_upload_warning, record_rate
    from app.services.settings import update_setting_dict

    big = _blank_pdf(tmp_path / "big.pdf", 600)
    small = _blank_pdf(tmp_path / "small.pdf", 2)
    assert estimate_pages(big) == (600, True)  # no text layer: scanned

    record_rate(db, embedder.name, "cpu", 100, 200)  # 0.5 chunks/s, like bge-m3 on a laptop CPU
    warning = large_upload_warning(db, [big])
    assert warning is not None and warning["pages"] == 600 and warning["scanned"]
    assert warning["current"]["measured"] and warning["current"]["seconds"] > 600
    cloud = {c["model"]: c for c in warning["cloud"]}
    assert cloud["text-embedding-3-small"]["seconds"] < warning["current"]["seconds"] / 3
    assert cloud["text-embedding-3-small"]["usd"] > 0
    assert large_upload_warning(db, [small]) is None

    update_setting_dict(db, "embedding", {"backend": "api", "provider": "openai", "model": "text-embedding-3-small"})
    assert large_upload_warning(db, [big]) is None  # cloud models are fast: no question asked


def test_indexing_records_speed(db: Session, embedder, make_client) -> None:
    from app.db.models import Page
    from app.indexer.indexer import Source, index_source
    from app.services.settings import get_setting

    client = make_client()
    text = "\n".join(f"Line {i} about parking, floors and opening hours." for i in range(400))
    page = Page(client_id=client.id, url="https://a.test", content=text, content_hash="x")
    db.add(page)
    db.commit()
    assert index_source(db, embedder, Source(client.id, "web", page.url, "A", text, page_id=page.id)) >= 8
    rates = get_setting(db, "embedding_rate")
    assert rates[embedder.name]["cpu"] > 0


def test_large_upload_is_held_until_the_admin_decides(db: Session, embedder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.services.embeddings import record_rate
    from tests.test_api import admin_client, finish_setup

    api = admin_client()
    finish_setup(db, api, monkeypatch)
    slug = api.post("/api/admin/clients", json={"client_id": "big", "name": "Big", "website_url": "https://big.test"}).json()["client"]["client_id"]
    record_rate(db, embedder.name, "cpu", 100, 200)
    db.commit()

    pdf = _blank_pdf(tmp_path / "manual.pdf", 600).read_bytes()
    res = api.post(f"/api/admin/clients/{slug}/documents", files={"files": ("manual.pdf", pdf, "application/pdf")}).json()
    assert res["job"] is None and res["large"]["pages"] == 600
    assert {c["model"] for c in res["large"]["cloud"]} >= {"text-embedding-3-small", "gemini-embedding-001"}
    docs = api.get(f"/api/admin/clients/{slug}/documents").json()["documents"]
    assert [d["status"] for d in docs] == ["pending"]

    job = api.post(f"/api/admin/clients/{slug}/documents/index").json()["job"]
    assert job["type"] == "index_docs"

    small = api.post(f"/api/admin/clients/{slug}/documents", files={"files": ("a.txt", b"Parking costs 40 rupees.", "text/plain")}).json()
    assert small["large"] is None and small["job"] is not None
