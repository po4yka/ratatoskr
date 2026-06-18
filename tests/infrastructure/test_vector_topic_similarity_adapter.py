from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import pytest

from app.application.services.signal_scoring import SignalCandidate
from app.infrastructure.embedding.embedding_protocol import pack_embedding, unpack_embedding
from app.infrastructure.search.vector_topic_similarity import VectorTopicSimilarityAdapter
from app.infrastructure.vector.result_types import VectorQueryHit, VectorQueryResult


def _candidate(
    *,
    title: str | None = "Vector search",
    canonical_url: str | None = "https://example.com/vector",
    metadata: dict[str, object] | None = None,
) -> SignalCandidate:
    return SignalCandidate(
        feed_item_id=11,
        source_id=1,
        source_kind="rss",
        title=title,
        canonical_url=canonical_url,
        metadata=metadata if metadata is not None else {"content_text": "semantic topic matching"},
    )


class _EmbeddingService:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def generate_embedding(
        self, text: str, *, language: str | None = None, task_type: str | None = None
    ) -> list[float]:
        kwargs = {"language": language, "task_type": task_type}
        self.calls.append({"text": text, **kwargs})
        if self.raises:
            raise RuntimeError("embedding failed")
        return [0.1, 0.2, 0.3]

    async def generate_embeddings_batch(
        self,
        texts: Sequence[str],
        *,
        language: str | None = None,
        task_type: str | None = None,
    ) -> list[list[float]]:
        return [
            await self.generate_embedding(text, language=language, task_type=task_type)
            for text in texts
        ]

    def serialize_embedding(self, embedding: Any) -> bytes:
        return pack_embedding(embedding)

    def deserialize_embedding(self, blob: bytes) -> list[float]:
        return unpack_embedding(blob)

    def get_model_name(self, language: str | None = None) -> str:
        del language
        return "fake-model"

    def get_dimensions(self, language: str | None = None) -> int:
        del language
        return 3

    def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _VectorStore:
    def __init__(self, *, available: bool = True, raises: bool = False) -> None:
        self.available = available
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def health_check(self) -> bool:
        return self.available

    def query(
        self,
        query_vector: Sequence[float],
        filters: dict[str, Any] | None,
        top_k: int,
    ) -> VectorQueryResult:
        self.calls.append({"query_vector": list(query_vector), "filters": filters, "top_k": top_k})
        if self.raises:
            raise RuntimeError("qdrant down")
        return VectorQueryResult(
            hits=[
                VectorQueryHit(id="1", distance=0.75, metadata={}),
                VectorQueryHit(id="2", distance=0.2, metadata={}),
                VectorQueryHit(id="3", distance=cast("float", "bad"), metadata={}),
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

    score = await adapter.score_item(_candidate())

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

    score = await adapter.score_item(_candidate(title="", canonical_url="", metadata={}))

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

    assert await embedding_fails.score_item(_candidate()) == 0.0
    assert await store_fails.score_item(_candidate()) == 0.0
