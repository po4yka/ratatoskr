"""The local embedding model load must not block the asyncio event loop.

SentenceTransformer(model_name) loads/downloads weights (seconds, worse on the
Pi). generate_embedding / generate_embeddings_batch must offload that first-use
load to a worker thread and load each model only once under concurrency.
sentence-transformers is not installed in unit CI, so the loader is faked and we
assert on the thread it runs in rather than doing a real load.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.infrastructure.embedding.embedding_service import EmbeddingService

pytestmark = pytest.mark.no_network


def _fake_loader(service: EmbeddingService, *, record: dict[str, Any], model: Any) -> Any:
    def _load(model_name: str) -> Any:
        record.setdefault("threads", []).append(threading.get_ident())
        record.setdefault("calls", []).append(model_name)
        service._models[model_name] = model
        service._dimensions[model_name] = 3
        return model

    return _load


@pytest.mark.asyncio
async def test_model_load_runs_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    service = EmbeddingService()
    fake_model = MagicMock()
    fake_model.encode.return_value = [0.1, 0.2, 0.3]
    record: dict[str, Any] = {}
    monkeypatch.setattr(
        service, "_ensure_model", _fake_loader(service, record=record, model=fake_model)
    )

    result = await service.generate_embedding("hello", language="en")

    # The blocking load ran in a worker thread, not the event-loop thread.
    assert record["threads"][0] != threading.get_ident()
    assert list(result) == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_cached_model_skips_thread_load(monkeypatch: pytest.MonkeyPatch) -> None:
    service = EmbeddingService()
    fake_model = MagicMock()
    fake_model.encode.return_value = [1.0]
    # Pre-warm the cache so the fast path is taken.
    service._models["all-MiniLM-L6-v2"] = fake_model
    service._dimensions["all-MiniLM-L6-v2"] = 1

    record: dict[str, Any] = {}
    monkeypatch.setattr(
        service, "_ensure_model", _fake_loader(service, record=record, model=fake_model)
    )

    await service.generate_embedding("hi", language="en")

    # Already-cached model must never re-enter the (blocking) loader.
    assert record.get("calls", []) == []


@pytest.mark.asyncio
async def test_concurrent_first_use_loads_model_once(monkeypatch: pytest.MonkeyPatch) -> None:
    service = EmbeddingService()
    fake_model = MagicMock()
    fake_model.encode.return_value = [1.0]
    record: dict[str, Any] = {}
    monkeypatch.setattr(
        service, "_ensure_model", _fake_loader(service, record=record, model=fake_model)
    )

    await asyncio.gather(*(service.generate_embedding("x", language="en") for _ in range(5)))

    # Five concurrent first-use calls, but the model is loaded exactly once.
    assert record.get("calls", []) == ["all-MiniLM-L6-v2"]


@pytest.mark.asyncio
async def test_batch_also_loads_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    service = EmbeddingService()
    fake_model = MagicMock()
    fake_model.encode.return_value = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    record: dict[str, Any] = {}
    monkeypatch.setattr(
        service, "_ensure_model", _fake_loader(service, record=record, model=fake_model)
    )

    out = await service.generate_embeddings_batch(["a", "b"], language="ru")

    assert record["threads"][0] != threading.get_ident()
    assert len(out) == 2
