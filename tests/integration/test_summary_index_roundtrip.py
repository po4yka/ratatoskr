"""T6 integration: read-your-writes summary index-on-write + byte-compat.

In-memory Qdrant (no external service, no Postgres). Proves the persist fast-path
writes a point the ground retrieval reads back IMMEDIATELY (no reconciler pass
needed), that the current request is excluded, that cross-scope summaries never
leak, and that the stored payload + point id are byte-identical to the shared
point shape (summary_point.py) so the reconciler sees no drift.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from qdrant_client import QdrantClient

from app.application.dto.vector_search import EntityType, RetrievalScope
from app.infrastructure.retrieval import QdrantRetrievalAdapter
from app.infrastructure.vector.point_ids import summary_point_id
from app.infrastructure.vector.qdrant_store import QdrantVectorStore
from app.infrastructure.vector.summary_index_adapter import QdrantSummaryIndexAdapter
from app.infrastructure.vector.summary_point import build_summary_qdrant_payload

if TYPE_CHECKING:
    from collections.abc import Generator

EMBEDDING_DIM = 8


class _FakeEmbedding:
    """Deterministic text-only embedding so document (write) and query (read)
    vectors match for the same text regardless of task_type/language."""

    async def generate_embedding(
        self, text: str, *, language: str | None = None, task_type: str | None = None
    ) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in digest[:EMBEDDING_DIM]]


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


def _summary(summary_id: int) -> dict[str, Any]:
    return {
        "title": f"Summary {summary_id}",
        "tldr": "A prior summary about consensus protocols and replication.",
        "summary_250": "Consensus protocols overview.",
        "summary_1000": "A longer overview of consensus and replication.",
        "source_type": "research",
        "topic_tags": ["#consensus", "#databases"],
        "url": f"https://example.com/{summary_id}",
    }


async def _index(
    store: QdrantVectorStore, *, request_id: int, summary_id: int, scope: RetrievalScope
) -> None:
    adapter = QdrantSummaryIndexAdapter(vector_store=store, embedding_service=_FakeEmbedding())
    await adapter.index_summary(
        request_id=request_id,
        summary_id=summary_id,
        summary=_summary(summary_id),
        lang="en",
        scope=scope,
        correlation_id="cid",
    )


def _retrieval(store: QdrantVectorStore) -> QdrantRetrievalAdapter:
    # SUMMARY retrieval does not hydrate, so db=None is fine.
    return QdrantRetrievalAdapter(vector_store=store, embedding_service=_FakeEmbedding(), db=None)


@pytest.mark.integration
async def test_read_your_writes_summary_is_retrievable_immediately(
    store: QdrantVectorStore,
) -> None:
    # Summaries are owner-wide at the vector layer: user_id=None (the point has none).
    scope = RetrievalScope(environment="test", user_scope="unit", user_id=None)
    await _index(store, request_id=1, summary_id=10, scope=scope)

    # No reconciler pass happened -- the fast-path point must already be queryable.
    result = await _retrieval(store).retrieve(
        entity_type=EntityType.SUMMARY, scope=scope, query="consensus", top_k=10
    )
    assert "10" in {hit.entity_id for hit in result.hits}


@pytest.mark.integration
async def test_current_request_is_excluded(store: QdrantVectorStore) -> None:
    scope = RetrievalScope(environment="test", user_scope="unit", user_id=None)
    await _index(store, request_id=1, summary_id=10, scope=scope)
    await _index(store, request_id=2, summary_id=20, scope=scope)

    result = await _retrieval(store).retrieve(
        entity_type=EntityType.SUMMARY,
        scope=scope,
        query="consensus",
        top_k=10,
        exclude_request_id=1,
    )
    ids = {hit.entity_id for hit in result.hits}
    assert "10" not in ids  # request 1's own summary excluded
    assert "20" in ids


@pytest.mark.integration
async def test_cross_scope_summary_never_returned(store: QdrantVectorStore) -> None:
    in_scope = RetrievalScope(environment="test", user_scope="unit", user_id=None)
    other_scope = RetrievalScope(environment="test", user_scope="other", user_id=None)
    other_env = RetrievalScope(environment="staging", user_scope="unit", user_id=None)
    await _index(store, request_id=1, summary_id=10, scope=in_scope)
    await _index(store, request_id=2, summary_id=20, scope=other_scope)
    await _index(store, request_id=3, summary_id=30, scope=other_env)

    result = await _retrieval(store).retrieve(
        entity_type=EntityType.SUMMARY, scope=in_scope, query="consensus", top_k=10
    )
    assert {hit.entity_id for hit in result.hits} == {"10"}  # scope isolation


@pytest.mark.integration
async def test_fastpath_payload_is_byte_compatible_with_shared_point_shape(
    store: QdrantVectorStore,
) -> None:
    scope = RetrievalScope(environment="test", user_scope="unit", user_id=7)
    await _index(store, request_id=1, summary_id=10, scope=scope)

    point_id = summary_point_id(1, 10)
    records = store._client.retrieve(
        collection_name=store._collection_name, ids=[point_id], with_payload=True
    )
    assert len(records) == 1, "fast-path point not found at the shared point id"

    expected = build_summary_qdrant_payload(10, 1, "en", _summary(10), "unit", "test")
    # Byte-identical to what the reconciler would write for the same summary:
    # same point id (asserted by the lookup above) + same payload (no drift).
    # Both the fast path and the reconciler build the point from summary_point.py,
    # so the reconciler sees no drift after the fast path has written.
    # NOTE (T7 closure): this builds `expected` from the same in-memory summary
    # dict the fast-path uses. When T7 lands persist's actual summary-row write,
    # add a test that builds the point from the persisted `json_payload` column
    # (the shape the reconciler reads) and asserts equality -- closing the last gap
    # between `state["summary"]` and the stored JSON.
    assert records[0].payload == expected
