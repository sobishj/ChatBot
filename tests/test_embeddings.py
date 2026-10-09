"""Embedder device selection (no database, no real model)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.embeddings import model as m

READY = {"model": "test/model", "path": "/models/test", "status": "ready"}


class StubEmbedder:
    """Stands in for SentenceTransformerEmbedder; records each load."""

    loads: list[str] = []
    fail_on_gpu = False

    def __init__(self, name: str, path: str, device: str = "cpu") -> None:
        if device == "cuda" and StubEmbedder.fail_on_gpu:
            raise RuntimeError("CUDA out of memory")
        StubEmbedder.loads.append(device)
        self.name, self.device, self.dim = name, device, 8


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[StubEmbedder]]:
    StubEmbedder.loads, StubEmbedder.fail_on_gpu = [], False
    monkeypatch.setattr(m, "SentenceTransformerEmbedder", StubEmbedder)
    monkeypatch.setattr(m, "_gpu_warned", True)  # keep test logs quiet
    m.set_embedder_for_tests(None)
    yield StubEmbedder
    m.set_embedder_for_tests(None)


def test_gpu_falls_back_to_cpu_without_a_gpu(stub, monkeypatch) -> None:
    monkeypatch.setattr(m, "_gpu_info", None)
    assert m.resolve_device("gpu") == "cpu"
    assert m.resolve_device("cpu") == "cpu"
    embedder = m.get_embedder(READY, "gpu")
    assert embedder.device == "cpu"


def test_switching_device_reloads_once(stub, monkeypatch) -> None:
    monkeypatch.setattr(m, "_gpu_info", {"name": "Test GPU", "memory_gb": 8.0, "cuda": "12.4"})
    monkeypatch.setattr(m, "_unload", lambda: setattr(m, "_embedder", None))  # no torch needed
    assert m.get_embedder(READY, "cpu").device == "cpu"
    assert m.get_embedder(READY, "cpu").device == "cpu"  # cached
    assert m.get_embedder(READY, "gpu").device == "cuda"
    assert m.get_embedder(READY, "gpu").device == "cuda"  # cached
    assert m.get_embedder(READY, "cpu").device == "cpu"
    assert stub.loads == ["cpu", "cuda", "cpu"]
    assert m.current_device() == "cpu"


def test_failed_gpu_load_uses_cpu_and_is_not_retried(stub, monkeypatch) -> None:
    monkeypatch.setattr(m, "_gpu_info", {"name": "Test GPU", "memory_gb": 2.0, "cuda": "12.4"})
    stub.fail_on_gpu = True
    assert m.get_embedder(READY, "gpu").device == "cpu"
    assert m.get_embedder(READY, "gpu").device == "cpu"
    assert stub.loads == ["cpu"]


def test_compute_device_setting_validation() -> None:
    from fastapi import HTTPException

    from app.admin.routes.settings import _validate

    assert _validate("compute_device", "gpu") == "gpu"
    assert _validate("compute_device", "cpu") == "cpu"
    with pytest.raises(HTTPException):
        _validate("compute_device", "tpu")
