from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from qdrant_client import QdrantClient

from app.infrastructure.vector.protocol import VectorStoreError
from app.infrastructure.vector.qdrant_store import QdrantVectorStore


class _Client:
    def __init__(self) -> None:
        self.upserts: list[Any] = []
        self.deletes: list[Any] = []
        self.closed = False
        self.fail_upsert = False
        self.fail_query = False
        self.records = [
            SimpleNamespace(
                id="point-1",
                payload={
                    "summary_id": "10",
                    "repository_id": "20",
                    "wiki_path": "docs/a.md",
                    "content_hash": "hash-a",
                },
            ),
            SimpleNamespace(
                id="point-2",
                payload={
                    "summary_id": "bad",
                    "repository_id": None,
                    "wiki_path": "",
                    "content_hash": "",
                },
            ),
        ]

    def upsert(self, **kwargs: Any) -> None:
        if self.fail_upsert:
            raise RuntimeError("upsert failed")
        self.upserts.append(kwargs)

    def query_points(self, **_kwargs: Any) -> Any:
        if self.fail_query:
            raise RuntimeError("query failed")
        return SimpleNamespace(
            points=[
                SimpleNamespace(
                    id="hit-1",
                    score=0.75,
                    payload={"summary_id": 10},
                )
            ]
        )

    def delete(self, **kwargs: Any) -> None:
        self.deletes.append(kwargs)

    def scroll(self, **_kwargs: Any) -> tuple[list[Any], None]:
        return self.records, None

    def get_collections(self) -> None:
        return None

    def delete_collection(self, _collection_name: str) -> None:
        return None

    def create_collection(self, **_kwargs: Any) -> None:
        return None

    def count(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(count=7)

    def close(self) -> None:
        self.closed = True


def _store(client: _Client | None = None, *, required: bool = False) -> QdrantVectorStore:
    store = object.__new__(QdrantVectorStore)
    store._url = "http://qdrant.test"
    store._api_key = None
    store._environment = "dev"
    store._user_scope = "user"
    store._collection_version = "v1"
    store._embedding_space = "space"
    store._embedding_dim = 3
    store._required = required
    store._connection_timeout = 1.0
    store._available = client is not None
    store._client = cast("QdrantClient | None", client)
    store._collection_name = "notes_dev_user_v1_space"
    return store


def test_qdrant_store_properties_and_helpers() -> None:
    store = _store(_Client())

    assert store.available is True
    assert store.environment == "dev"
    assert store.user_scope == "user"
    assert store.collection_version == "v1"
    assert store.embedding_space == "space"
    assert store.collection_name == "notes_dev_user_v1_space"
    assert (
        QdrantVectorStore._build_collection_name("dev env", "user scope", "v 1", "Model/Name")
        == "notes_dev_env_user_scope_v_1_model_name"
    )
    assert QdrantVectorStore._extract_id({"request_id": 1}) == "1"
    assert QdrantVectorStore._extract_id({"request_id": 1, "chunk_id": "c"}) == "1:c"
    assert QdrantVectorStore._extract_id({"request_id": 1, "window_id": "w"}) == "1:w"
    assert QdrantVectorStore._extract_id({"request_id": 1, "summary_id": 2}) == "1:2"
    assert QdrantVectorStore._extract_id({}) != ""

    points = store._build_points(
        [[0.1, 0.2, 0.3]],
        [{"request_id": 1, "tags": [], "language": "en"}],
        ["1"],
    )
    assert points[0].payload == {
        "request_id": 1,
        "language": "en",
        "environment": "dev",
        "user_scope": "user",
    }


def test_qdrant_store_upsert_replace_query_and_read_indexes() -> None:
    client = _Client()
    store = _store(client)

    store.upsert_notes([[0.1, 0.2, 0.3]], [{"request_id": 1, "summary_id": 10}])
    assert len(client.upserts) == 1

    store.replace_request_notes(
        1,
        [[0.1, 0.2, 0.3]],
        [{"request_id": 1, "summary_id": 10}],
        ids=["1:10"],
    )
    assert len(client.upserts) == 2

    result = store.query([0.1, 0.2, 0.3], {"language": "en", "environment": "ignored"}, 5)
    assert result.hits[0].id == "hit-1"
    assert result.hits[0].distance == 0.25
    assert result.hits[0].metadata == {"summary_id": 10}

    store.delete_by_request_id(1)
    store.delete_x_wiki_paths(["docs/a.md"])
    assert len(client.deletes) >= 2
    assert store.health_check() is True
    assert store.get_indexed_summary_ids() == {10}
    assert store.get_indexed_repository_ids() == {20}
    assert store.get_indexed_x_wiki_paths() == {"docs/a.md"}
    assert store.get_indexed_x_wiki_path_hashes() == {"docs/a.md": "hash-a"}
    assert store.count() == 7
    store.reset()
    store.close()
    assert store.available is False
    assert client.closed is True


def test_qdrant_store_validates_inputs_and_handles_unavailable() -> None:
    store = _store(None)
    store.ensure_available = lambda: False  # type: ignore[method-assign]

    store.upsert_notes([[0.1]], [{"request_id": 1}])
    store.replace_request_notes(1, [[0.1]], [{"request_id": 1}])
    assert store.query([0.1], None, 1).hits == []
    store.delete_by_request_id(1)
    assert store.get_indexed_summary_ids() == set()
    assert store.get_indexed_repository_ids() == set()
    assert store.get_indexed_x_wiki_paths() == set()
    assert store.get_indexed_x_wiki_path_hashes() == {}
    store.delete_x_wiki_paths([])
    store.delete_x_wiki_paths(["docs/a.md"])
    assert store.count() == 0

    available_store = _store(_Client())
    with pytest.raises(ValueError, match="same length"):
        available_store.upsert_notes([[0.1]], [])
    with pytest.raises(ValueError, match="ids must have"):
        available_store.upsert_notes([[0.1]], [{"request_id": 1}], ids=["a", "b"])
    with pytest.raises(ValueError, match="top_k"):
        available_store.query([0.1], None, 0)


def test_qdrant_store_failure_paths_respect_required_flag() -> None:
    client = _Client()
    client.fail_upsert = True
    store = _store(client)

    store.upsert_notes([[0.1]], [{"request_id": 1}], ids=["1"])
    assert store.available is False

    required_store = _store(client, required=True)
    with pytest.raises(VectorStoreError):
        required_store.upsert_notes([[0.1]], [{"request_id": 1}], ids=["1"])

    query_client = _Client()
    query_client.fail_query = True
    query_store = _store(query_client)
    assert query_store.query([0.1], None, 1).hits == []
    assert query_store.available is False


def test_upsert_notes_chunks_large_batches_and_forwards_wait() -> None:
    client = _Client()
    store = _store(client)

    count = 600  # > 2 * the 256-point chunk size
    vectors = [[0.1, 0.2, 0.3] for _ in range(count)]
    metadatas = [{"request_id": i, "summary_id": i} for i in range(count)]

    store.upsert_notes(vectors, metadatas, wait=False)

    # ceil(600 / 256) = 3 chunks, none larger than the chunk size.
    assert len(client.upserts) == 3
    assert [len(call["points"]) for call in client.upserts] == [256, 256, 88]
    # wait is forwarded to every chunk.
    assert all(call["wait"] is False for call in client.upserts)
    # All points are written exactly once.
    assert sum(len(call["points"]) for call in client.upserts) == count


def test_upsert_notes_defaults_to_wait_true() -> None:
    client = _Client()
    store = _store(client)
    store.upsert_notes([[0.1, 0.2, 0.3]], [{"request_id": 1, "summary_id": 1}])
    assert client.upserts[0]["wait"] is True
