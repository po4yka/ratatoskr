"""CachedEmbeddingService wires EmbeddingCache into the single-text embed path."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.infrastructure.embedding.cached_embedding_service import CachedEmbeddingService
from app.infrastructure.embedding.embedding_protocol import EmbeddingServiceProtocol


class _FakeInner:
    """Minimal EmbeddingServiceProtocol impl that records single-text calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.closed = False
        self.aclosed = False

    async def generate_embedding(
        self, text: str, *, language: str | None = None, task_type: str | None = None
    ) -> Any:
        self.calls.append((text, language, task_type))
        return [1.0, 2.0, 3.0]

    async def generate_embeddings_batch(
        self, texts: Any, *, language: str | None = None, task_type: str | None = None
    ) -> list[Any]:
        return [[9.0] for _ in texts]

    def serialize_embedding(self, embedding: Any) -> bytes:
        return b"blob"

    def deserialize_embedding(self, blob: bytes) -> list[float]:
        return [0.5]

    def get_model_name(self, language: str | None = None) -> str:
        return f"model-{language or 'default'}"

    def get_dimensions(self, language: str | None = None) -> int:
        return 3

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.aclosed = True


def test_satisfies_embedding_service_protocol() -> None:
    svc = CachedEmbeddingService(_FakeInner(), MagicMock())
    assert isinstance(svc, EmbeddingServiceProtocol)


@pytest.mark.asyncio
async def test_generate_embedding_routes_through_cache_keyed_by_model() -> None:
    inner = _FakeInner()
    cache = MagicMock()
    cache.get_or_compute = AsyncMock(return_value=[7.0, 8.0])
    svc = CachedEmbeddingService(inner, cache)

    out = await svc.generate_embedding("hello", language="en", task_type="document")

    assert out == [7.0, 8.0]
    cache.get_or_compute.assert_awaited_once()
    text_arg, model_arg, compute_fn = cache.get_or_compute.await_args.args
    assert text_arg == "hello"
    assert model_arg == "model-en"  # partitioned by the inner service's model
    # The compute_fn recomputes via the inner service with the same kwargs.
    assert await compute_fn("hello") == [1.0, 2.0, 3.0]
    assert inner.calls == [("hello", "en", "document")]


@pytest.mark.asyncio
async def test_cache_hit_never_invokes_inner() -> None:
    inner = _FakeInner()
    cache = MagicMock()
    # Simulate a hit: get_or_compute returns without ever calling compute_fn.
    cache.get_or_compute = AsyncMock(return_value=[42.0])
    svc = CachedEmbeddingService(inner, cache)

    assert await svc.generate_embedding("x") == [42.0]
    assert inner.calls == []


@pytest.mark.asyncio
async def test_batch_delegates_to_inner_and_bypasses_single_text_cache() -> None:
    inner = _FakeInner()
    cache = MagicMock()
    cache.get_or_compute = AsyncMock()
    svc = CachedEmbeddingService(inner, cache)

    out = await svc.generate_embeddings_batch(["a", "b"], language="ru")

    assert out == [[9.0], [9.0]]
    # Batch keeps the provider's own batch call -- not fanned out through the cache.
    cache.get_or_compute.assert_not_awaited()


def test_sync_methods_delegate_to_inner() -> None:
    inner = _FakeInner()
    svc = CachedEmbeddingService(inner, MagicMock())

    assert svc.serialize_embedding([1.0]) == b"blob"
    assert svc.deserialize_embedding(b"x") == [0.5]
    assert svc.get_model_name("en") == "model-en"
    assert svc.get_dimensions() == 3
    assert svc.inner is inner

    svc.close()
    assert inner.closed is True


@pytest.mark.asyncio
async def test_aclose_delegates_to_inner() -> None:
    inner = _FakeInner()
    svc = CachedEmbeddingService(inner, MagicMock())
    await svc.aclose()
    assert inner.aclosed is True
