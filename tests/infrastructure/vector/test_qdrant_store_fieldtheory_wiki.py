"""Unit tests for QdrantVectorStore fieldtheory_wiki helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointIdsList

from app.infrastructure.vector.point_ids import str_to_uuid
from app.infrastructure.vector.qdrant_store import QdrantVectorStore

EMBEDDING_DIM = 3


class _FakeQdrantClient:
    """Minimal fake honoring the QdrantClient surface used by ``_try_connect``.

    Captures the ``scroll_filter`` passed to ``scroll`` so tests can assert on the
    constructed Filter without standing up a real Qdrant instance.
    """

    def __init__(self, *, scroll_records: list[Any]) -> None:
        self._scroll_records = scroll_records
        self.scroll_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def get_collections(self) -> None:
        return None

    def collection_exists(self, _name: str) -> bool:
        return True  # skip create_collection branch

    def scroll(
        self,
        *,
        collection_name: str,
        scroll_filter: Filter,
        limit: int,
        with_payload: list[str],
        with_vectors: bool,
    ) -> tuple[list[Any], None]:
        self.scroll_calls.append(
            {
                "collection_name": collection_name,
                "scroll_filter": scroll_filter,
                "limit": limit,
                "with_payload": with_payload,
                "with_vectors": with_vectors,
            }
        )
        return self._scroll_records, None

    def delete(
        self,
        *,
        collection_name: str,
        points_selector: Any,
        wait: bool,
    ) -> None:
        self.delete_calls.append(
            {
                "collection_name": collection_name,
                "points_selector": points_selector,
                "wait": wait,
            }
        )


@pytest.fixture
def fake_client() -> _FakeQdrantClient:
    return _FakeQdrantClient(
        scroll_records=[
            SimpleNamespace(
                payload={
                    "entity_type": "fieldtheory_wiki",
                    "wiki_path": "/fieldtheory/library/alpha.md",
                }
            ),
            SimpleNamespace(
                payload={
                    "entity_type": "fieldtheory_wiki",
                    "wiki_path": "/fieldtheory/library/beta.md",
                }
            ),
            SimpleNamespace(payload={"entity_type": "repository", "repository_id": 7}),
            SimpleNamespace(payload={"entity_type": "fieldtheory_wiki"}),  # missing path
            SimpleNamespace(payload={"entity_type": "fieldtheory_wiki", "wiki_path": None}),
        ]
    )


@pytest.fixture
def store_with_fake(
    fake_client: _FakeQdrantClient,
) -> tuple[QdrantVectorStore, _FakeQdrantClient]:
    """QdrantVectorStore wired to a deterministic in-process fake client."""
    with patch(
        "app.infrastructure.vector.qdrant_store.QdrantClient",
        return_value=fake_client,
    ):
        s = QdrantVectorStore(
            url="http://localhost:6333",
            api_key=None,
            environment="test",
            user_scope="unit",
            embedding_dim=EMBEDDING_DIM,
        )
    assert s.available
    return s, fake_client


def test_returns_only_wiki_path_strings_from_mixed_payloads(
    store_with_fake: tuple[QdrantVectorStore, _FakeQdrantClient],
) -> None:
    store, _client = store_with_fake

    paths = store.get_indexed_fieldtheory_wiki_paths()

    assert paths == {
        "/fieldtheory/library/alpha.md",
        "/fieldtheory/library/beta.md",
    }
    assert all(isinstance(p, str) for p in paths)


def test_constructs_filter_with_entity_type_fieldtheory_wiki(
    store_with_fake: tuple[QdrantVectorStore, _FakeQdrantClient],
) -> None:
    store, client = store_with_fake

    store.get_indexed_fieldtheory_wiki_paths(user_id=42)

    assert len(client.scroll_calls) == 1
    call = client.scroll_calls[0]
    scroll_filter = call["scroll_filter"]
    assert isinstance(scroll_filter, Filter)
    must = list(scroll_filter.must or [])

    entity_type_condition = FieldCondition(
        key="entity_type", match=MatchValue(value="fieldtheory_wiki")
    )
    assert entity_type_condition in must

    environment_condition = FieldCondition(key="environment", match=MatchValue(value="test"))
    user_scope_condition = FieldCondition(key="user_scope", match=MatchValue(value="unit"))
    user_id_condition = FieldCondition(key="user_id", match=MatchValue(value=42))
    assert environment_condition in must
    assert user_scope_condition in must
    assert user_id_condition in must

    assert call["with_payload"] == ["wiki_path"]
    assert call["with_vectors"] is False


def test_returns_empty_set_when_store_unavailable() -> None:
    with patch(
        "app.infrastructure.vector.qdrant_store.QdrantClient",
        side_effect=RuntimeError("connection refused"),
    ):
        store = QdrantVectorStore(
            url="http://bad-host:6333",
            api_key=None,
            environment="test",
            user_scope="unit",
            embedding_dim=EMBEDDING_DIM,
            required=False,
        )

    assert not store.available

    with patch.object(QdrantVectorStore, "ensure_available", return_value=False) as ensure:
        paths = store.get_indexed_fieldtheory_wiki_paths()

    assert paths == set()
    assert ensure.called


def test_returns_empty_set_when_scroll_raises() -> None:
    failing_client = MagicMock()
    failing_client.get_collections.return_value = None
    failing_client.collection_exists.return_value = True
    failing_client.scroll.side_effect = RuntimeError("qdrant offline mid-scan")

    with patch(
        "app.infrastructure.vector.qdrant_store.QdrantClient",
        return_value=failing_client,
    ):
        store = QdrantVectorStore(
            url="http://localhost:6333",
            api_key=None,
            environment="test",
            user_scope="unit",
            embedding_dim=EMBEDDING_DIM,
        )

    paths = store.get_indexed_fieldtheory_wiki_paths()

    assert paths == set()


@pytest.fixture
def fake_client_with_hashes() -> _FakeQdrantClient:
    return _FakeQdrantClient(
        scroll_records=[
            SimpleNamespace(
                payload={
                    "entity_type": "fieldtheory_wiki",
                    "wiki_path": "/fieldtheory/library/alpha.md",
                    "content_hash": "hash-a",
                }
            ),
            SimpleNamespace(
                payload={
                    "entity_type": "fieldtheory_wiki",
                    "wiki_path": "/fieldtheory/library/beta.md",
                    "content_hash": "hash-b",
                }
            ),
            SimpleNamespace(
                payload={
                    "entity_type": "fieldtheory_wiki",
                    "wiki_path": "/fieldtheory/library/no-hash.md",
                    # content_hash missing — must be dropped
                }
            ),
            SimpleNamespace(
                payload={
                    "entity_type": "fieldtheory_wiki",
                    "wiki_path": None,
                    "content_hash": "hash-x",
                }
            ),
        ]
    )


def test_path_hashes_returns_only_paths_with_hash(
    fake_client_with_hashes: _FakeQdrantClient,
) -> None:
    with patch(
        "app.infrastructure.vector.qdrant_store.QdrantClient",
        return_value=fake_client_with_hashes,
    ):
        store = QdrantVectorStore(
            url="http://localhost:6333",
            api_key=None,
            environment="test",
            user_scope="unit",
            embedding_dim=EMBEDDING_DIM,
        )

    result = store.get_indexed_fieldtheory_wiki_path_hashes()

    assert result == {
        "/fieldtheory/library/alpha.md": "hash-a",
        "/fieldtheory/library/beta.md": "hash-b",
    }
    assert len(fake_client_with_hashes.scroll_calls) == 1
    call = fake_client_with_hashes.scroll_calls[0]
    assert call["with_payload"] == ["wiki_path", "content_hash"]


def test_path_hashes_returns_empty_when_scroll_raises() -> None:
    failing_client = MagicMock()
    failing_client.get_collections.return_value = None
    failing_client.collection_exists.return_value = True
    failing_client.scroll.side_effect = RuntimeError("qdrant offline mid-scan")

    with patch(
        "app.infrastructure.vector.qdrant_store.QdrantClient",
        return_value=failing_client,
    ):
        store = QdrantVectorStore(
            url="http://localhost:6333",
            api_key=None,
            environment="test",
            user_scope="unit",
            embedding_dim=EMBEDDING_DIM,
        )

    assert store.get_indexed_fieldtheory_wiki_path_hashes() == {}


def test_delete_fieldtheory_wiki_paths_uses_deterministic_uuids(
    fake_client_with_hashes: _FakeQdrantClient,
) -> None:
    with patch(
        "app.infrastructure.vector.qdrant_store.QdrantClient",
        return_value=fake_client_with_hashes,
    ):
        store = QdrantVectorStore(
            url="http://localhost:6333",
            api_key=None,
            environment="test",
            user_scope="unit",
            embedding_dim=EMBEDDING_DIM,
        )

    paths = ["/fieldtheory/library/alpha.md", "/fieldtheory/library/beta.md"]
    store.delete_fieldtheory_wiki_paths(paths)

    assert len(fake_client_with_hashes.delete_calls) == 1
    call = fake_client_with_hashes.delete_calls[0]
    selector = call["points_selector"]
    assert isinstance(selector, PointIdsList)
    assert selector.points == [str_to_uuid(p) for p in paths]
    assert call["wait"] is True


def test_delete_fieldtheory_wiki_paths_noop_on_empty(
    fake_client_with_hashes: _FakeQdrantClient,
) -> None:
    with patch(
        "app.infrastructure.vector.qdrant_store.QdrantClient",
        return_value=fake_client_with_hashes,
    ):
        store = QdrantVectorStore(
            url="http://localhost:6333",
            api_key=None,
            environment="test",
            user_scope="unit",
            embedding_dim=EMBEDDING_DIM,
        )

    store.delete_fieldtheory_wiki_paths([])

    assert fake_client_with_hashes.delete_calls == []
