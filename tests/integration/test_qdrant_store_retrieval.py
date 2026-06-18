"""Integration tests for the T4 store primitives: query_filter + find_similar_by_id.

Runs against an in-memory Qdrant client (no external service / no Postgres).
Repository points are seeded via the raw client so the payload matches the shared
point shape (the high-level upsert_notes path validates summary metadata).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    HasIdCondition,
    MatchValue,
    PointStruct,
)

from app.application.dto.vector_search import EntityType, RetrievalScope
from app.infrastructure.retrieval import QdrantRetrievalAdapter
from app.infrastructure.vector.qdrant_store import QdrantVectorStore

if TYPE_CHECKING:
    from collections.abc import Generator

EMBEDDING_DIM = 3

# Deterministic point UUIDs (v5-shaped) so we can address the seed precisely.
PID_A = "11111111-1111-5111-8111-111111111111"
PID_B = "22222222-2222-5222-8222-222222222222"
PID_OTHER_USER = "33333333-3333-5333-8333-333333333333"
PID_OTHER_ENV = "44444444-4444-5444-8444-444444444444"
PID_OTHER_SCOPE = "55555555-5555-5555-8555-555555555555"


def _adapter() -> QdrantRetrievalAdapter:
    # _build_filter uses no injected deps, so None placeholders are fine here.
    return QdrantRetrievalAdapter(vector_store=None, embedding_service=None, db=None)


def _make_in_memory_client(**_kwargs: object) -> QdrantClient:
    return QdrantClient(":memory:")


@pytest.fixture
def store() -> Generator[QdrantVectorStore]:
    with patch(
        "app.infrastructure.vector.qdrant_store.QdrantClient",
        side_effect=_make_in_memory_client,
    ):
        instance = QdrantVectorStore(
            url="http://localhost:6333",
            api_key=None,
            environment="test",
            user_scope="unit",
            embedding_dim=EMBEDDING_DIM,
        )
    assert instance.available
    yield instance
    instance.close()


def _repo_payload(repository_id: int, user_id: int) -> dict[str, object]:
    return {
        "entity_type": "repository",
        "repository_id": repository_id,
        "user_id": user_id,
        "environment": "test",
        "user_scope": "unit",
    }


def _seed(store: QdrantVectorStore, points: list[PointStruct]) -> None:
    store._client.upsert(collection_name=store._collection_name, points=points, wait=True)


def _repo_filter(user_id: int, *, exclude: str | None = None) -> Filter:
    must: list[Any] = [
        FieldCondition(key="entity_type", match=MatchValue(value="repository")),
        FieldCondition(key="user_id", match=MatchValue(value=user_id)),
        FieldCondition(key="environment", match=MatchValue(value="test")),
        FieldCondition(key="user_scope", match=MatchValue(value="unit")),
    ]
    must_not: list[Any] | None = [HasIdCondition(has_id=[exclude])] if exclude else None
    return Filter(must=must, must_not=must_not)


@pytest.mark.integration
def test_query_filter_enforces_scope(store: QdrantVectorStore) -> None:
    _seed(
        store,
        [
            PointStruct(id=PID_A, vector=[0.9, 0.1, 0.0], payload=_repo_payload(1, 1)),
            PointStruct(id=PID_B, vector=[0.85, 0.15, 0.0], payload=_repo_payload(2, 1)),
            PointStruct(id=PID_OTHER_USER, vector=[0.9, 0.1, 0.0], payload=_repo_payload(3, 2)),
        ],
    )
    result = store.query_filter([0.9, 0.1, 0.0], _repo_filter(user_id=1), top_k=10)
    returned = {hit.metadata.get("repository_id") for hit in result.hits}
    assert returned == {1, 2}  # user 2's repository excluded by the scope filter
    assert all(hit.distance >= 0.0 for hit in result.hits)


@pytest.mark.integration
def test_query_filter_score_threshold(store: QdrantVectorStore) -> None:
    _seed(
        store,
        [
            PointStruct(id=PID_A, vector=[1.0, 0.0, 0.0], payload=_repo_payload(1, 1)),
            PointStruct(id=PID_B, vector=[0.0, 1.0, 0.0], payload=_repo_payload(2, 1)),
        ],
    )
    # Query aligned with PID_A; a high threshold drops the orthogonal PID_B.
    result = store.query_filter(
        [1.0, 0.0, 0.0], _repo_filter(user_id=1), top_k=10, score_threshold=0.9
    )
    assert {hit.metadata.get("repository_id") for hit in result.hits} == {1}


@pytest.mark.integration
def test_find_similar_by_id_excludes_seed(store: QdrantVectorStore) -> None:
    _seed(
        store,
        [
            PointStruct(id=PID_A, vector=[0.9, 0.1, 0.0], payload=_repo_payload(1, 1)),
            PointStruct(id=PID_B, vector=[0.88, 0.12, 0.0], payload=_repo_payload(2, 1)),
            PointStruct(id=PID_OTHER_USER, vector=[0.2, 0.8, 0.0], payload=_repo_payload(3, 1)),
        ],
    )
    result = store.find_similar_by_id(PID_A, _repo_filter(user_id=1, exclude=PID_A), top_k=10)
    returned_ids = {hit.id for hit in result.hits}
    assert PID_A not in returned_ids  # seed excluded via must_not HasId
    assert PID_B in returned_ids  # the near neighbour is returned


# ---------------------------------------------------------------------------
# End-to-end: the ADAPTER's own _build_filter driving real Qdrant. These close
# the seam where the unit tests (fake store) and store tests (hand-built filter)
# never met -- here a bug in _build_filter (wrong key/match type) WOULD fail.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_adapter_filter_excludes_cross_scope_points(store: QdrantVectorStore) -> None:
    same_vector = [0.9, 0.1, 0.0]
    _seed(
        store,
        [
            # only this one is user_id=1, environment=test, user_scope=unit
            PointStruct(id=PID_A, vector=same_vector, payload=_repo_payload(1, 1)),
            PointStruct(id=PID_B, vector=same_vector, payload=_repo_payload(2, 2)),  # other user
            PointStruct(
                id=PID_OTHER_ENV,
                vector=same_vector,
                payload={**_repo_payload(3, 1), "environment": "other"},
            ),
            PointStruct(
                id=PID_OTHER_SCOPE,
                vector=same_vector,
                payload={**_repo_payload(4, 1), "user_scope": "other"},
            ),
        ],
    )
    scope = RetrievalScope(environment="test", user_scope="unit", user_id=1)
    qdrant_filter = _adapter()._build_filter(EntityType.REPOSITORY, scope, None)
    result = store.query_filter(same_vector, qdrant_filter, top_k=10)
    # The adapter's filter alone must exclude the other-user / other-env / other-scope points.
    assert {hit.metadata.get("repository_id") for hit in result.hits} == {1}


@pytest.mark.integration
def test_adapter_find_similar_filter_excludes_seed(store: QdrantVectorStore) -> None:
    _seed(
        store,
        [
            PointStruct(id=PID_A, vector=[0.9, 0.1, 0.0], payload=_repo_payload(1, 1)),
            PointStruct(id=PID_B, vector=[0.88, 0.12, 0.0], payload=_repo_payload(2, 1)),
        ],
    )
    scope = RetrievalScope(environment="test", user_scope="unit", user_id=1)
    qdrant_filter = _adapter()._build_filter(
        EntityType.REPOSITORY, scope, None, exclude_point_id=PID_A
    )
    result = store.find_similar_by_id(PID_A, qdrant_filter, top_k=10)
    returned_ids = {hit.id for hit in result.hits}
    assert PID_A not in returned_ids  # adapter's must_not HasId honored by Qdrant
    assert PID_B in returned_ids
