from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from app.infrastructure.search.vector_topic_similarity import VectorTopicSimilarityAdapter


@dataclass
class _Candidate:
    feed_item_id: int = 11
    title: str | None = "Vector search"
    canonical_url: str | None = "https://example.com/vector"
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {"content_text": "semantic topic matching"}


class _EmbeddingService:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def generate_embedding(self, text: str, **kwargs: Any) -> list[float]:
        self.calls.append({"text": text, **kwargs})
        if self.raises:
            raise RuntimeError("embedding failed")
        return [0.1, 0.2, 0.3]


class _VectorStore:
    def __init__(self, *, available: bool = True, raises: bool = False) -> None:
        self.available = available
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def health_check(self) -> bool:
        return self.available

    def query(
        self,
        query_vector: list[float],
        filters: dict[str, Any] | None,
        top_k: int,
    ) -> SimpleNamespace:
        self.calls.append({"query_vector": query_vector, "filters": filters, "top_k": top_k})
        if self.raises:
            raise RuntimeError("qdrant down")
        return SimpleNamespace(
            hits=[
                SimpleNamespace(distance=0.75),
                SimpleNamespace(distance=0.2),
                SimpleNamespace(distance="bad"),
            ]
        )


def test_vector_topic_similarity_rejects_non_positive_top_k() -> None:
    with pytest.raises(ValueError, match="top_k"):
        VectorTopicSimilarityAdapter(
            vector_store=_VectorStore(),
            embedding_service=_EmbeddingService(),
            top_k=0,
        )


def test_vector_topic_similarity_readiness_prefers_health_check() -> None:
    adapter = VectorTopicSimilarityAdapter(
        vector_store=_VectorStore(available=False),
        embedding_service=_EmbeddingService(),
    )

    assert adapter.is_ready() is False


@pytest.mark.asyncio
async def test_vector_topic_similarity_scores_best_hit_and_filters_by_user() -> None:
    vector_store = _VectorStore()
    embedding_service = _EmbeddingService()
    adapter = VectorTopicSimilarityAdapter(
        vector_store=vector_store,
        embedding_service=embedding_service,
        user_id=77,
        top_k=3,
    )

    score = await adapter.score_item(_Candidate())

    assert score == pytest.approx(0.8)
    assert embedding_service.calls[0]["task_type"] == "query"
    assert "Vector search" in embedding_service.calls[0]["text"]
    assert "semantic topic matching" in embedding_service.calls[0]["text"]
    assert vector_store.calls == [
        {"query_vector": [0.1, 0.2, 0.3], "filters": {"user_id": 77}, "top_k": 3}
    ]


@pytest.mark.asyncio
async def test_vector_topic_similarity_empty_candidate_returns_zero_without_embedding() -> None:
    embedding_service = _EmbeddingService()
    adapter = VectorTopicSimilarityAdapter(
        vector_store=_VectorStore(),
        embedding_service=embedding_service,
    )

    score = await adapter.score_item(_Candidate(title="", canonical_url="", metadata={}))

    assert score == 0.0
    assert embedding_service.calls == []


@pytest.mark.asyncio
async def test_vector_topic_similarity_degrades_to_zero_on_embedding_or_store_error() -> None:
    embedding_fails = VectorTopicSimilarityAdapter(
        vector_store=_VectorStore(),
        embedding_service=_EmbeddingService(raises=True),
    )
    store_fails = VectorTopicSimilarityAdapter(
        vector_store=_VectorStore(raises=True),
        embedding_service=_EmbeddingService(),
    )

    assert await embedding_fails.score_item(_Candidate()) == 0.0
    assert await store_fails.score_item(_Candidate()) == 0.0
