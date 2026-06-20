from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from app.di import search as search_di
from tests.api.dependencies.search_resources_helpers import (
    get_test_vector_service,
    set_vector_factories,
    shutdown_test_vector_service,
)


class DummyEmbeddingService:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.close()


class DummyVectorStore:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.close()


@pytest.mark.asyncio
async def test_vector_resource_manager_reuses_singleton_and_shuts_down():
    set_vector_factories(
        embedding_factory=DummyEmbeddingService,
        vector_store_factory=lambda config: DummyVectorStore(),
        config_factory=lambda: SimpleNamespace(
            host="http://localhost",
            auth_token=None,
            environment="test",
            user_scope="user",
            collection_version="v1",
        ),
    )

    try:
        first = await get_test_vector_service()
        second = await get_test_vector_service()

        assert first is second

        embedding = getattr(first, "_embedding_service", None)
        vector_store = getattr(first, "_vector_store", None)

        await shutdown_test_vector_service()

        assert embedding.closed is True
        assert vector_store.closed is True

        fresh = await get_test_vector_service()
        assert fresh is not first
    finally:
        set_vector_factories()
        await shutdown_test_vector_service()


@pytest.mark.asyncio
async def test_vector_resource_test_factories_build_service_without_api_runtime():
    set_vector_factories(
        embedding_factory=DummyEmbeddingService,
        vector_store_factory=lambda config: DummyVectorStore(),
        config_factory=lambda: SimpleNamespace(host="http://localhost"),
    )

    try:
        service = await get_test_vector_service()
        assert getattr(service, "_embedding_service", None) is not None
        assert getattr(service, "_vector_store", None) is not None
    finally:
        set_vector_factories()
        await shutdown_test_vector_service()


def test_build_search_dependencies_raises_when_vector_store_is_required(monkeypatch) -> None:
    class FailingStore:
        def __init__(self, **_kwargs) -> None:
            raise RuntimeError("store unavailable")

    monkeypatch.setattr(
        "app.infrastructure.vector.qdrant_store.QdrantVectorStore",
        FailingStore,
    )

    cfg = SimpleNamespace(
        runtime=SimpleNamespace(topic_search_max_results=5, request_timeout_sec=5.0),
        vector_store=SimpleNamespace(
            url="http://localhost:6333",
            api_key=None,
            environment="test",
            user_scope="public",
            collection_version="v1",
            required=True,
            connection_timeout=3.0,
        ),
        embedding=SimpleNamespace(provider="local", max_token_length=512, embedding_dim=768),
    )

    with pytest.raises(RuntimeError, match="store unavailable"):
        search_di.build_search_dependencies(
            cast("Any", cfg),
            db=MagicMock(),
            llm_client=MagicMock(),
            audit_func=lambda *_args, **_kwargs: None,
        )
