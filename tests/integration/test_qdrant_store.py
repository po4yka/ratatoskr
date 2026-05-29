"""Integration tests for QdrantVectorStore using an in-memory Qdrant client."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from qdrant_client import QdrantClient

from app.infrastructure.vector.qdrant_store import QdrantVectorStore
from app.infrastructure.vector.result_types import VectorQueryResult

if TYPE_CHECKING:
    from collections.abc import Generator

EMBEDDING_DIM = 3


def _make_in_memory_client(**_kwargs: object) -> QdrantClient:
    """Factory used to replace `QdrantClient(url=..., ...)` with an in-memory instance."""
    return QdrantClient(":memory:")


@pytest.fixture
def store() -> Generator[QdrantVectorStore]:
    with patch(
        "app.infrastructure.vector.qdrant_store.QdrantClient",
        side_effect=_make_in_memory_client,
    ):
        s = QdrantVectorStore(
            url="http://localhost:6333",
            api_key=None,
            environment="test",
            user_scope="unit",
            embedding_dim=EMBEDDING_DIM,
        )
    assert s.available
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vec(seed: float) -> list[float]:
    """Non-collinear vectors so cosine distance is meaningful between calls."""
    return [seed, 1.0 - seed, seed * 0.5]


def _meta(request_id: int, summary_id: int, **extra: object) -> dict:
    return {"request_id": request_id, "summary_id": summary_id, **extra}


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_health_check(store: QdrantVectorStore) -> None:
    assert store.health_check() is True


# ---------------------------------------------------------------------------
# collection_name
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_collection_name_scheme(store: QdrantVectorStore) -> None:
    assert store.collection_name == "notes_test_unit_v1"


@pytest.mark.integration
def test_collection_name_with_embedding_space() -> None:
    with patch(
        "app.infrastructure.vector.qdrant_store.QdrantClient",
        side_effect=_make_in_memory_client,
    ):
        s = QdrantVectorStore(
            url="http://localhost:6333",
            api_key=None,
            environment="prod",
            user_scope="public",
            embedding_space="gemini-embedding-2-preview_768d",
            embedding_dim=EMBEDDING_DIM,
        )
    assert s.collection_name == "notes_prod_public_v1_gemini-embedding-2-preview_768d"
    s.close()


# ---------------------------------------------------------------------------
# upsert_notes + query
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_upsert_and_query_returns_hits(store: QdrantVectorStore) -> None:
    vectors = [_vec(0.1), _vec(0.4)]
    metadatas = [_meta(1, 11), _meta(2, 22)]
    store.upsert_notes(vectors, metadatas)

    result = store.query(query_vector=_vec(0.1), filters=None, top_k=2)

    assert isinstance(result, VectorQueryResult)
    assert len(result.hits) == 2
    # Closest match first — the identical vector should score highest (distance ~ 0)
    assert result.hits[0].distance == pytest.approx(0.0, abs=1e-5)


@pytest.mark.integration
def test_query_top_k_limits_results(store: QdrantVectorStore) -> None:
    for i in range(5):
        store.upsert_notes([[float(i), float(i), float(i)]], [_meta(i, i)])

    result = store.query(query_vector=[1.0, 1.0, 1.0], filters=None, top_k=3)
    assert len(result.hits) <= 3


@pytest.mark.integration
def test_query_distance_is_non_negative(store: QdrantVectorStore) -> None:
    store.upsert_notes([_vec(0.9)], [_meta(10, 100)])
    result = store.query(query_vector=_vec(0.1), filters=None, top_k=1)
    assert all(h.distance >= 0.0 for h in result.hits)


@pytest.mark.integration
def test_upsert_is_idempotent(store: QdrantVectorStore) -> None:
    vectors = [_vec(0.5)]
    metadatas = [_meta(99, 999)]
    store.upsert_notes(vectors, metadatas)
    store.upsert_notes(vectors, metadatas)  # second upsert with same ID
    assert store.count() == 1


# ---------------------------------------------------------------------------
# delete_by_request_id
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_delete_by_request_id(store: QdrantVectorStore) -> None:
    store.upsert_notes([_vec(0.1), _vec(0.2)], [_meta(1, 11), _meta(2, 22)])
    assert store.count() == 2

    store.delete_by_request_id(1)
    assert store.count() == 1

    # The remaining point belongs to request_id=2
    result = store.query(query_vector=_vec(0.2), filters=None, top_k=5)
    assert all(h.metadata.get("request_id") != 1 for h in result.hits)


# ---------------------------------------------------------------------------
# replace_request_notes
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_replace_request_notes_removes_stale_points(store: QdrantVectorStore) -> None:
    # Initial: two chunks for request 1
    store.upsert_notes(
        [_vec(0.1), _vec(0.2)],
        [_meta(1, 10, chunk_id="c0"), _meta(1, 10, chunk_id="c1")],
    )
    assert store.count() == 2

    # Replace with a single chunk — the stale one should be removed
    store.replace_request_notes(
        1,
        [_vec(0.3)],
        [_meta(1, 10, chunk_id="c0")],
    )
    assert store.count() == 1


@pytest.mark.integration
def test_replace_request_notes_does_not_touch_other_requests(store: QdrantVectorStore) -> None:
    store.upsert_notes(
        [_vec(0.1), _vec(0.5)],
        [_meta(1, 10), _meta(2, 20)],
    )
    store.replace_request_notes(1, [_vec(0.2)], [_meta(1, 10)])

    # request_id=2 must survive untouched
    result = store.query(query_vector=_vec(0.5), filters={"request_id": 2}, top_k=5)
    assert any(h.metadata.get("request_id") == 2 for h in result.hits)


# ---------------------------------------------------------------------------
# get_indexed_summary_ids
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_indexed_summary_ids_returns_inserted(store: QdrantVectorStore) -> None:
    store.upsert_notes(
        [_vec(0.1), _vec(0.2), _vec(0.3)],
        [_meta(1, 11), _meta(2, 22), _meta(3, 33)],
    )
    ids = store.get_indexed_summary_ids()
    assert {11, 22, 33}.issubset(ids)


@pytest.mark.integration
def test_get_indexed_summary_ids_paginates_beyond_one_page(store: QdrantVectorStore) -> None:
    """More points than a single scroll page must all be returned (no truncation)."""
    count = 5500  # > the default 5000-point scroll page size
    vectors = [[float(i % 7) * 0.1, 0.2, 0.3] for i in range(1, count + 1)]
    metadatas = [_meta(i, i) for i in range(1, count + 1)]
    store.upsert_notes(vectors, metadatas)

    ids = store.get_indexed_summary_ids()
    assert len(ids) == count
    assert ids == set(range(1, count + 1))


@pytest.mark.integration
def test_get_indexed_summary_ids_filters_by_user_id(store: QdrantVectorStore) -> None:
    store.upsert_notes(
        [_vec(0.1), _vec(0.2)],
        [_meta(1, 11, user_id=1001), _meta(2, 22, user_id=1002)],
    )
    ids_1001 = store.get_indexed_summary_ids(user_id=1001)
    assert 11 in ids_1001
    assert 22 not in ids_1001


@pytest.mark.integration
def test_get_indexed_repository_ids_returns_inserted(store: QdrantVectorStore) -> None:
    store.upsert_notes(
        [_vec(0.1), _vec(0.2)],
        [
            {"entity_type": "repository", "repository_id": 101, "user_id": 1001},
            {"entity_type": "repository", "repository_id": 102, "user_id": 1002},
        ],
    )

    ids_1001 = store.get_indexed_repository_ids(user_id=1001)

    assert 101 in ids_1001
    assert 102 not in ids_1001


# ---------------------------------------------------------------------------
# count + reset
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_count(store: QdrantVectorStore) -> None:
    assert store.count() == 0
    store.upsert_notes([_vec(0.1)], [_meta(1, 11)])
    assert store.count() == 1


@pytest.mark.integration
def test_reset_clears_collection(store: QdrantVectorStore) -> None:
    store.upsert_notes([_vec(0.1), _vec(0.2)], [_meta(1, 11), _meta(2, 22)])
    assert store.count() == 2
    store.reset()
    assert store.count() == 0


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_unavailable_store_query_returns_empty() -> None:
    with patch(
        "app.infrastructure.vector.qdrant_store.QdrantClient",
        side_effect=RuntimeError("connection refused"),
    ):
        s = QdrantVectorStore(
            url="http://bad-host:6333",
            api_key=None,
            environment="test",
            user_scope="unit",
            embedding_dim=EMBEDDING_DIM,
            required=False,
        )
    assert not s.available
    result = s.query(query_vector=[0.1, 0.2, 0.3], filters=None, top_k=5)
    assert result == VectorQueryResult.empty()


@pytest.mark.integration
def test_required_true_raises_on_connection_failure() -> None:
    from app.infrastructure.vector.protocol import VectorStoreError

    with pytest.raises(VectorStoreError):
        with patch(
            "app.infrastructure.vector.qdrant_store.QdrantClient",
            side_effect=RuntimeError("refused"),
        ):
            QdrantVectorStore(
                url="http://bad-host:6333",
                api_key=None,
                environment="test",
                user_scope="unit",
                embedding_dim=EMBEDDING_DIM,
                required=True,
            )
