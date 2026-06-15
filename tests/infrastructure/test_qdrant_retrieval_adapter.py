"""Unit tests for QdrantRetrievalAdapter (T4 unified retrieval, ADR-0016).

DB-free. Covers the security-critical pieces -- the centralized, structurally
unbypassable scope filter and the find_similar by-id primitive -- with a fake
vector store that records calls. Per-entity Postgres hydration and summary
find_similar (which need a live DB) are validated by the cutover parity net.
"""

from __future__ import annotations

from typing import Any

import pytest
from qdrant_client.models import FieldCondition, HasIdCondition

from app.application.dto.vector_search import EntityType, RetrievalScope
from app.application.ports.retrieval import RetrievalPort
from app.infrastructure.retrieval import QdrantRetrievalAdapter
from app.infrastructure.vector.point_ids import repository_point_id
from app.infrastructure.vector.result_types import VectorQueryHit, VectorQueryResult

SCOPE = RetrievalScope(environment="prod", user_scope="public", user_id=7)


class _FakeStore:
    def __init__(self, hits: list[VectorQueryHit] | None = None) -> None:
        self._hits = hits or []
        self.last_query_filter: dict[str, Any] | None = None
        self.last_find_similar: dict[str, Any] | None = None

    def query_filter(
        self, query_vector: Any, qdrant_filter: Any, top_k: int, *, score_threshold: Any = None
    ) -> VectorQueryResult:
        self.last_query_filter = {
            "vector": query_vector,
            "filter": qdrant_filter,
            "top_k": top_k,
            "score_threshold": score_threshold,
        }
        return VectorQueryResult(hits=list(self._hits))

    def find_similar_by_id(
        self, point_id: str, qdrant_filter: Any, top_k: int, *, score_threshold: Any = None
    ) -> VectorQueryResult:
        self.last_find_similar = {"point_id": point_id, "filter": qdrant_filter, "top_k": top_k}
        return VectorQueryResult(hits=list(self._hits))


class _FakeEmbedding:
    async def generate_embedding(
        self, text: str, *, language: str | None = None, task_type: str = "document"
    ) -> list[float]:
        return [0.1, 0.2, 0.3]


def _adapter(hits: list[VectorQueryHit] | None = None) -> QdrantRetrievalAdapter:
    return QdrantRetrievalAdapter(
        vector_store=_FakeStore(hits), embedding_service=_FakeEmbedding(), db=None
    )


def _must_keys(qdrant_filter: Any) -> set[str]:
    return {c.key for c in (qdrant_filter.must or []) if isinstance(c, FieldCondition)}


def test_adapter_satisfies_retrieval_port() -> None:
    assert isinstance(_adapter(), RetrievalPort)


def test_scope_filter_always_present_per_entity_type() -> None:
    adapter = _adapter()
    for entity_type in EntityType:
        keys = _must_keys(adapter._build_filter(entity_type, SCOPE, None))
        assert "environment" in keys, entity_type
        assert "user_scope" in keys, entity_type


def test_user_id_filter_for_user_scoped_entities() -> None:
    adapter = _adapter()
    for entity_type in (EntityType.SUMMARY, EntityType.REPOSITORY, EntityType.GIT_MIRROR):
        assert "user_id" in _must_keys(adapter._build_filter(entity_type, SCOPE, None)), entity_type
    # x_wiki is environment + user_scope scoped only (no per-user partition).
    assert "user_id" not in _must_keys(adapter._build_filter(EntityType.X_WIKI, SCOPE, None))


def test_entity_type_condition_only_for_non_summary() -> None:
    adapter = _adapter()
    # Summary points are identified by carrying summary_id (legacy points predate
    # the entity_type field), so no positive entity_type match is added.
    assert "entity_type" not in _must_keys(adapter._build_filter(EntityType.SUMMARY, SCOPE, None))
    for entity_type in (EntityType.REPOSITORY, EntityType.GIT_MIRROR, EntityType.X_WIKI):
        assert "entity_type" in _must_keys(adapter._build_filter(entity_type, SCOPE, None))


def test_repository_optional_filters_match_legacy_shape() -> None:
    adapter = _adapter()
    qdrant_filter = adapter._build_filter(
        EntityType.REPOSITORY,
        SCOPE,
        {"languages": ["python"], "topics": ["ai", "ml"], "is_starred": True, "source": "manual"},
    )
    assert {"primary_language", "is_starred", "source"} <= _must_keys(qdrant_filter)
    # topics map to a MinShould(min_count=1) over per-topic conditions.
    assert qdrant_filter.min_should is not None
    assert len(qdrant_filter.should) == 2


def test_retrieval_scope_requires_user_scope() -> None:
    with pytest.raises(TypeError):
        RetrievalScope(environment="prod")  # type: ignore[call-arg]


async def test_repository_retrieve_requires_user_id() -> None:
    adapter = _adapter()
    scope = RetrievalScope(environment="prod", user_scope="public", user_id=None)
    with pytest.raises(ValueError, match="user_id"):
        await adapter.retrieve(
            entity_type=EntityType.REPOSITORY, scope=scope, vector=[0.1, 0.2, 0.3]
        )


async def test_retrieve_requires_query_or_vector() -> None:
    with pytest.raises(ValueError, match="query or vector"):
        await _adapter().retrieve(entity_type=EntityType.SUMMARY, scope=SCOPE)


async def test_summary_retrieve_maps_score_and_distance() -> None:
    hit = VectorQueryHit(
        id="pid-1", distance=0.25, metadata={"summary_id": 5, "request_id": 9, "title": "t"}
    )
    store = _FakeStore([hit])
    adapter = QdrantRetrievalAdapter(
        vector_store=store, embedding_service=_FakeEmbedding(), db=None
    )
    result = await adapter.retrieve(entity_type=EntityType.SUMMARY, scope=SCOPE, query="hello")
    assert result.total == 1
    only = result.hits[0]
    assert only.entity_type is EntityType.SUMMARY
    assert only.entity_id == "5"
    assert only.point_id == "pid-1"
    assert only.score == pytest.approx(0.75)  # 1 - distance
    assert only.distance == pytest.approx(0.25)
    assert store.last_query_filter is not None
    assert {"environment", "user_scope"} <= _must_keys(store.last_query_filter["filter"])


async def test_summary_retrieve_skips_hits_without_entity_id() -> None:
    hits = [
        VectorQueryHit(id="a", distance=0.1, metadata={"summary_id": 1}),
        VectorQueryHit(id="b", distance=0.2, metadata={"repository_id": 99}),  # not a summary
    ]
    adapter = QdrantRetrievalAdapter(
        vector_store=_FakeStore(hits), embedding_service=_FakeEmbedding(), db=None
    )
    result = await adapter.retrieve(entity_type=EntityType.SUMMARY, scope=SCOPE, query="x")
    assert [h.entity_id for h in result.hits] == ["1"]


async def test_find_similar_repository_excludes_seed_and_scopes() -> None:
    store = _FakeStore([])  # empty -> no hydration / no DB access
    adapter = QdrantRetrievalAdapter(
        vector_store=store, embedding_service=_FakeEmbedding(), db=None
    )
    result = await adapter.find_similar(
        entity_type=EntityType.REPOSITORY, entity_id="42", scope=SCOPE
    )
    assert result.total == 0
    expected_point_id = repository_point_id(SCOPE.environment, SCOPE.user_scope, 42)
    assert store.last_find_similar is not None
    assert store.last_find_similar["point_id"] == expected_point_id
    qdrant_filter = store.last_find_similar["filter"]
    has_id_conditions = [c for c in (qdrant_filter.must_not or []) if isinstance(c, HasIdCondition)]
    assert has_id_conditions and expected_point_id in has_id_conditions[0].has_id
    assert {"environment", "user_scope", "user_id"} <= _must_keys(qdrant_filter)


async def test_find_similar_x_wiki_unsupported() -> None:
    with pytest.raises(ValueError, match="not supported"):
        await _adapter().find_similar(
            entity_type=EntityType.X_WIKI, entity_id="path/x", scope=SCOPE
        )


async def test_rerank_reorders_via_injected_reranker() -> None:
    hits = [
        VectorQueryHit(id=f"p{i}", distance=0.1 * i, metadata={"summary_id": i, "title": f"t{i}"})
        for i in range(3)
    ]

    class _ReverseReranker:
        async def rerank(self, query: str, documents: list[Any], **_kwargs: Any) -> list[Any]:
            return list(reversed(documents))

    adapter = QdrantRetrievalAdapter(
        vector_store=_FakeStore(hits),
        embedding_service=_FakeEmbedding(),
        db=None,
        reranker=_ReverseReranker(),
    )
    result = await adapter.retrieve(
        entity_type=EntityType.SUMMARY, scope=SCOPE, query="x", rerank=True
    )
    # Reranker reversed the candidate order; default rerank=False would keep 0,1,2.
    assert [h.entity_id for h in result.hits] == ["2", "1", "0"]
