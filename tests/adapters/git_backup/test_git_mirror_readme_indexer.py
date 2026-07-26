"""Hermetic tests for GitMirrorReadmeIndexer and GitMirrorSearchService.

Covers:
- git_mirror_point_id determinism
- Indexer skips empty README
- Indexer dedup: same hash -> no re-embed, no Qdrant upsert
- Indexer re-embeds on changed hash
- Indexer sets correct entity_type payload
- GitMirrorSearchService hydrates and orders results by Qdrant score
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.infrastructure.embedding.embedding_protocol import pack_embedding, unpack_embedding
from app.infrastructure.vector.point_ids import git_mirror_point_id
from app.infrastructure.vector.qdrant_store import QdrantVectorStore

# ---------------------------------------------------------------------------
# point_id determinism
# ---------------------------------------------------------------------------


def test_git_mirror_point_id_deterministic() -> None:
    pid1 = git_mirror_point_id("prod", "owner", 42)
    pid2 = git_mirror_point_id("prod", "owner", 42)
    assert pid1 == pid2


def test_git_mirror_point_id_differs_by_mirror_id() -> None:
    pid1 = git_mirror_point_id("prod", "owner", 1)
    pid2 = git_mirror_point_id("prod", "owner", 2)
    assert pid1 != pid2


def test_git_mirror_point_id_differs_by_environment() -> None:
    pid1 = git_mirror_point_id("prod", "owner", 1)
    pid2 = git_mirror_point_id("staging", "owner", 1)
    assert pid1 != pid2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mirror(
    *,
    mirror_id: int = 1,
    user_id: int = 42,
    repository_id: int | None = None,
    readme_content_hash: str | None = None,
    name: str = "my-mirror",
    clone_url: str = "https://example.com/repo.git",
) -> MagicMock:
    mirror = MagicMock()
    mirror.id = mirror_id
    mirror.user_id = user_id
    mirror.repository_id = repository_id
    mirror.readme_content_hash = readme_content_hash
    mirror.name = name
    mirror.clone_url = clone_url
    return mirror


class FakeEmbeddingService:
    def __init__(self, vector: list[float] | None = None) -> None:
        self._vector = vector or [0.1, 0.2, 0.3]
        self.call_count = 0

    async def generate_embedding(
        self, text: str, *, language: str | None = None, task_type: str | None = None
    ) -> list[float]:
        del text, language, task_type
        self.call_count += 1
        return self._vector

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
        return len(self._vector)

    def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class FakeQdrantStore(QdrantVectorStore):
    def __init__(self, available: bool = True, *, acknowledged: bool = True) -> None:
        self._available = available
        self._acknowledged = acknowledged
        self.upserted: list[tuple[list[Any], list[Any], list[Any]]] = []

    @property
    def available(self) -> bool:
        return self._available

    def upsert_notes(
        self,
        vectors: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, Any]],
        ids: Sequence[str] | None = None,
        *,
        wait: bool = True,
    ) -> bool:
        del wait
        self.upserted.append((list(vectors), list(metadatas), list(ids or [])))
        return self._acknowledged


def _make_db_with_transaction() -> MagicMock:
    """Fake Database that captures UPDATE executions."""
    executed: list[Any] = []

    mock_session = AsyncMock()

    async def execute(stmt: Any) -> MagicMock:
        executed.append(stmt)
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result

    mock_session.execute = AsyncMock(side_effect=execute)

    transaction_ctx = MagicMock()

    async def _aenter(self: Any) -> Any:
        return mock_session

    async def _aexit(self: Any, *args: Any) -> None:
        pass

    transaction_ctx.__aenter__ = _aenter
    transaction_ctx.__aexit__ = _aexit

    db = MagicMock()
    db.transaction.return_value = transaction_ctx
    db._executed = executed
    return db


# ---------------------------------------------------------------------------
# GitMirrorReadmeIndexer tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_indexer_skips_empty_readme() -> None:
    from app.infrastructure.search.git_mirror_readme_indexer import GitMirrorReadmeIndexer

    embedding_service = FakeEmbeddingService()
    qdrant = FakeQdrantStore()
    db = _make_db_with_transaction()

    fake_extractor = MagicMock()
    fake_extractor.extract.return_value = ""

    indexer = GitMirrorReadmeIndexer(
        embedding_service=embedding_service,
        qdrant_store=qdrant,
        db=db,
        environment="prod",
        user_scope="owner",
        readme_extractor=fake_extractor,
    )

    mirror = _make_mirror()
    await indexer.index_mirror(mirror, Path("/fake/path"))

    assert embedding_service.call_count == 0
    assert len(qdrant.upserted) == 0


@pytest.mark.asyncio
async def test_indexer_skips_on_same_hash() -> None:
    """Same content hash -> no re-embed, no Qdrant upsert."""
    import hashlib

    from app.infrastructure.search.git_mirror_readme_indexer import GitMirrorReadmeIndexer

    readme_text = "# My README"
    content_hash = hashlib.sha256(readme_text.encode()).hexdigest()

    embedding_service = FakeEmbeddingService()
    qdrant = FakeQdrantStore()
    db = _make_db_with_transaction()

    fake_extractor = MagicMock()
    fake_extractor.extract.return_value = readme_text

    indexer = GitMirrorReadmeIndexer(
        embedding_service=embedding_service,
        qdrant_store=qdrant,
        db=db,
        environment="prod",
        user_scope="owner",
        readme_extractor=fake_extractor,
    )

    mirror = _make_mirror(readme_content_hash=content_hash)
    await indexer.index_mirror(mirror, Path("/fake/path"))

    assert embedding_service.call_count == 0
    assert len(qdrant.upserted) == 0


@pytest.mark.asyncio
async def test_indexer_embeds_on_changed_hash() -> None:
    """Changed content hash -> embed + Qdrant upsert."""
    from app.infrastructure.search.git_mirror_readme_indexer import GitMirrorReadmeIndexer

    readme_text = "# Updated README"

    embedding_service = FakeEmbeddingService()
    qdrant = FakeQdrantStore()
    db = _make_db_with_transaction()

    fake_extractor = MagicMock()
    fake_extractor.extract.return_value = readme_text

    indexer = GitMirrorReadmeIndexer(
        embedding_service=embedding_service,
        qdrant_store=qdrant,
        db=db,
        environment="prod",
        user_scope="owner",
        readme_extractor=fake_extractor,
    )

    # Old hash is different from the current content.
    mirror = _make_mirror(readme_content_hash="old-hash-value")
    await indexer.index_mirror(mirror, Path("/fake/path"))

    assert embedding_service.call_count == 1
    assert len(qdrant.upserted) == 1


@pytest.mark.asyncio
async def test_indexer_does_not_persist_hash_without_qdrant_ack() -> None:
    from app.infrastructure.search.git_mirror_readme_indexer import GitMirrorReadmeIndexer

    embedding_service = FakeEmbeddingService()
    qdrant = FakeQdrantStore(acknowledged=False)
    db = _make_db_with_transaction()
    fake_extractor = MagicMock()
    fake_extractor.extract.return_value = "# Updated README"
    indexer = GitMirrorReadmeIndexer(
        embedding_service=embedding_service,
        qdrant_store=qdrant,
        db=db,
        environment="prod",
        user_scope="owner",
        readme_extractor=fake_extractor,
    )

    await indexer.index_mirror(
        _make_mirror(readme_content_hash="old-hash-value"),
        Path("/fake/path"),
    )

    assert len(qdrant.upserted) == 1
    db.transaction.assert_not_called()


@pytest.mark.asyncio
async def test_indexer_embeds_when_no_previous_hash() -> None:
    """No previous hash -> embed + Qdrant upsert."""
    from app.infrastructure.search.git_mirror_readme_indexer import GitMirrorReadmeIndexer

    readme_text = "# First README"

    embedding_service = FakeEmbeddingService()
    qdrant = FakeQdrantStore()
    db = _make_db_with_transaction()

    fake_extractor = MagicMock()
    fake_extractor.extract.return_value = readme_text

    indexer = GitMirrorReadmeIndexer(
        embedding_service=embedding_service,
        qdrant_store=qdrant,
        db=db,
        environment="prod",
        user_scope="owner",
        readme_extractor=fake_extractor,
    )

    mirror = _make_mirror(readme_content_hash=None)
    await indexer.index_mirror(mirror, Path("/fake/path"))

    assert embedding_service.call_count == 1
    assert len(qdrant.upserted) == 1


@pytest.mark.asyncio
async def test_indexer_payload_entity_type() -> None:
    """Qdrant upsert payload must have entity_type='git_mirror'."""
    from app.infrastructure.search.git_mirror_readme_indexer import GitMirrorReadmeIndexer

    readme_text = "# README with content"

    embedding_service = FakeEmbeddingService()
    qdrant = FakeQdrantStore()
    db = _make_db_with_transaction()

    fake_extractor = MagicMock()
    fake_extractor.extract.return_value = readme_text

    indexer = GitMirrorReadmeIndexer(
        embedding_service=embedding_service,
        qdrant_store=qdrant,
        db=db,
        environment="test-env",
        user_scope="test-scope",
        readme_extractor=fake_extractor,
    )

    mirror = _make_mirror(mirror_id=77, user_id=99, name="test-repo", readme_content_hash=None)
    await indexer.index_mirror(mirror, Path("/fake/path"))

    assert len(qdrant.upserted) == 1
    _vectors, metadatas, point_ids = qdrant.upserted[0]
    payload = metadatas[0]
    assert payload["entity_type"] == "git_mirror"
    assert payload["mirror_id"] == 77
    assert payload["user_id"] == 99
    assert payload["name"] == "test-repo"
    assert payload["environment"] == "test-env"
    assert payload["user_scope"] == "test-scope"
    assert payload["language"] == "en"

    # Point ID must match the deterministic function.
    expected_pid = git_mirror_point_id("test-env", "test-scope", 77)
    assert point_ids[0] == expected_pid


@pytest.mark.asyncio
async def test_indexer_skips_when_qdrant_unavailable() -> None:
    """When Qdrant is unavailable, no embedding or upsert occurs."""
    from app.infrastructure.search.git_mirror_readme_indexer import GitMirrorReadmeIndexer

    readme_text = "# README"

    embedding_service = FakeEmbeddingService()
    qdrant = FakeQdrantStore(available=False)
    db = _make_db_with_transaction()

    fake_extractor = MagicMock()
    fake_extractor.extract.return_value = readme_text

    indexer = GitMirrorReadmeIndexer(
        embedding_service=embedding_service,
        qdrant_store=qdrant,
        db=db,
        environment="prod",
        user_scope="owner",
        readme_extractor=fake_extractor,
    )

    mirror = _make_mirror(readme_content_hash=None)
    await indexer.index_mirror(mirror, Path("/fake/path"))

    assert embedding_service.call_count == 0
    assert len(qdrant.upserted) == 0


# ---------------------------------------------------------------------------
# GitMirrorSearchService tests
# ---------------------------------------------------------------------------


def _make_search_qdrant(hits: list[dict[str, Any]]) -> MagicMock:
    """Fake qdrant_store for search: query_points returns structured hits."""
    point_mocks = []
    for h in hits:
        p = MagicMock()
        p.score = h["score"]
        p.payload = h["payload"]
        point_mocks.append(p)

    response = MagicMock()
    response.points = point_mocks

    client = MagicMock()
    client.query_points.return_value = response

    store = MagicMock()
    store.available = True
    store._client = client
    store._collection_name = "test_collection"
    return store


def _make_search_db(mirrors: list[MagicMock]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = mirrors

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    ctx = MagicMock()

    async def _aenter(self: Any) -> Any:
        return session

    async def _aexit(self: Any, *args: Any) -> None:
        pass

    ctx.__aenter__ = _aenter
    ctx.__aexit__ = _aexit

    db = MagicMock()
    db.session.return_value = ctx
    return db


@pytest.mark.asyncio
async def test_search_service_orders_by_score() -> None:
    """Results are ordered by Qdrant score (highest score = lowest distance first)."""
    from app.infrastructure.search.git_mirror_search_service import GitMirrorSearchService

    hits = [
        {"score": 0.9, "payload": {"mirror_id": 1, "entity_type": "git_mirror"}},
        {"score": 0.7, "payload": {"mirror_id": 2, "entity_type": "git_mirror"}},
    ]
    qdrant = _make_search_qdrant(hits)

    mirror1 = _make_mirror(mirror_id=1)
    mirror1.status = MagicMock()
    mirror1.status.value = "ok"
    mirror1.source = MagicMock()
    mirror1.source.value = "manual"

    mirror2 = _make_mirror(mirror_id=2)
    mirror2.status = MagicMock()
    mirror2.status.value = "ok"
    mirror2.source = MagicMock()
    mirror2.source.value = "manual"

    db = _make_search_db([mirror1, mirror2])

    embedding_service = FakeEmbeddingService()
    service = GitMirrorSearchService(
        embedding_service=embedding_service,
        qdrant_store=qdrant,
        db=db,
        environment="prod",
        user_scope="owner",
    )

    results = await service.search("find repos", user_id=42, limit=10)

    assert len(results.items) == 2
    # First item has lower distance (higher similarity).
    assert results.items[0].distance < results.items[1].distance
    assert results.items[0].mirror_id == 1
    assert results.items[1].mirror_id == 2


@pytest.mark.asyncio
async def test_search_service_returns_empty_when_qdrant_unavailable() -> None:
    from app.infrastructure.search.git_mirror_search_service import GitMirrorSearchService

    qdrant = MagicMock()
    qdrant.available = False
    db = MagicMock()
    embedding_service = FakeEmbeddingService()

    service = GitMirrorSearchService(
        embedding_service=embedding_service,
        qdrant_store=qdrant,
        db=db,
        environment="prod",
        user_scope="owner",
    )

    results = await service.search("test query", user_id=42, limit=10)
    assert results.items == []
    assert results.total == 0


@pytest.mark.asyncio
async def test_search_service_returns_empty_on_no_hits() -> None:
    from app.infrastructure.search.git_mirror_search_service import GitMirrorSearchService

    qdrant = _make_search_qdrant([])
    db = _make_search_db([])
    embedding_service = FakeEmbeddingService()

    service = GitMirrorSearchService(
        embedding_service=embedding_service,
        qdrant_store=qdrant,
        db=db,
        environment="prod",
        user_scope="owner",
    )

    results = await service.search("nothing here", user_id=42, limit=10)
    assert results.items == []
