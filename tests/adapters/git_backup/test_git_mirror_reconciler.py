"""Hermetic tests for git-mirror vector reconciliation.

Covers:
- index_mirror(force=True) bypasses content-hash dedup
- GitMirrorVectorReconciler: orphan deletion, missing re-index (on disk),
  missing clear (gone from disk), skip when Qdrant unavailable
- QdrantVectorStore.delete_git_mirror_points point-id derivation
- QdrantVectorStore.get_indexed_git_mirror_ids payload parsing
- GitMirrorVectorIndexedEntityAdapter stat assembly (fake session)
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.infrastructure.embedding.embedding_protocol import pack_embedding, unpack_embedding
from app.infrastructure.vector.point_ids import git_mirror_point_id
from app.infrastructure.vector.qdrant_store import QdrantVectorStore

# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, session: Any) -> None:
        self._s = session

    async def __aenter__(self) -> Any:
        return self._s

    async def __aexit__(self, *_args: Any) -> bool:
        return False


class _FakeEmbedding:
    def __init__(self) -> None:
        self.call_count = 0

    async def generate_embedding(
        self, text: str, *, language: str | None = None, task_type: str | None = None
    ) -> list[float]:
        del text, language, task_type
        self.call_count += 1
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


class _FakeIndexQdrant(QdrantVectorStore):
    def __init__(self, available: bool = True) -> None:
        self._available = available
        self.upserted: list[Any] = []

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
        return True


def _index_db() -> MagicMock:
    session = MagicMock()

    async def _execute(stmt: Any) -> Any:
        return MagicMock()

    session.execute = _execute
    db = MagicMock()
    db.transaction.return_value = _Ctx(session)
    return db


def _mirror(**kw: Any) -> MagicMock:
    m = MagicMock()
    m.id = kw.get("mirror_id", 1)
    m.user_id = kw.get("user_id", 42)
    m.repository_id = kw.get("repository_id")
    m.readme_content_hash = kw.get("readme_content_hash")
    m.name = kw.get("name", "m")
    m.clone_url = kw.get("clone_url", "https://example.com/r.git")
    return m


# ---------------------------------------------------------------------------
# index_mirror(force=...)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_mirror_force_bypasses_dedup() -> None:
    from app.infrastructure.search.git_mirror_readme_indexer import GitMirrorReadmeIndexer

    readme = "# unchanged"
    same_hash = hashlib.sha256(readme.encode()).hexdigest()
    extractor = MagicMock()
    extractor.extract.return_value = readme

    embedding = _FakeEmbedding()
    qdrant = _FakeIndexQdrant()
    indexer = GitMirrorReadmeIndexer(
        embedding_service=embedding,
        qdrant_store=qdrant,
        db=_index_db(),
        environment="prod",
        user_scope="owner",
        readme_extractor=extractor,
    )
    mirror = _mirror(readme_content_hash=same_hash)

    # Without force: hash matches -> skip.
    await indexer.index_mirror(mirror, Path("/fake"))
    assert embedding.call_count == 0
    assert qdrant.upserted == []

    # With force: re-embed + upsert even though hash matches.
    await indexer.index_mirror(mirror, Path("/fake"), force=True)
    assert embedding.call_count == 1
    assert len(qdrant.upserted) == 1


# ---------------------------------------------------------------------------
# GitMirrorVectorReconciler
# ---------------------------------------------------------------------------


class _ReconSession:
    def __init__(self, rows: list[Any], mirrors: dict[int, Any], log: list[Any]) -> None:
        self._rows = rows
        self._mirrors = mirrors
        self._log = log

    async def execute(self, stmt: Any) -> Any:
        self._log.append(stmt)
        res = MagicMock()
        res.all.return_value = self._rows
        return res

    async def get(self, _model: Any, id_: int) -> Any:
        return self._mirrors.get(id_)


class _ReconDb:
    def __init__(self, rows: list[Any], mirrors: dict[int, Any]) -> None:
        self._rows = rows
        self._mirrors = mirrors
        self.exec_log: list[Any] = []

    def session(self) -> _Ctx:
        return _Ctx(_ReconSession(self._rows, self._mirrors, self.exec_log))

    def transaction(self) -> _Ctx:
        return _Ctx(_ReconSession(self._rows, self._mirrors, self.exec_log))


class _ReconQdrant:
    def __init__(self, indexed: set[int], available: bool = True) -> None:
        self.available = available
        self._indexed = indexed
        self.deleted: list[list[int]] = []

    def get_indexed_git_mirror_ids(self, *, limit: int | None = None) -> set[int]:
        return set(self._indexed)

    def delete_git_mirror_points(self, ids: Any) -> None:
        self.deleted.append(list(ids))


class _ReconIndexer:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, Path, bool]] = []

    async def index_mirror(self, mirror: Any, path: Path, *, force: bool = False) -> None:
        self.calls.append((mirror, path, force))


def _build_reconciler(db: Any, qdrant: Any, indexer: Any) -> Any:
    from app.infrastructure.search.git_mirror_reconciler import GitMirrorVectorReconciler

    return GitMirrorVectorReconciler(db=db, qdrant_store=qdrant, indexer=indexer)


@pytest.mark.asyncio
async def test_reconciler_deletes_orphans() -> None:
    rows = [SimpleNamespace(id=1, mirror_path="/exists")]
    db = _ReconDb(rows, {1: _mirror(mirror_id=1)})
    qdrant = _ReconQdrant(indexed={1, 99})  # 99 has no DB row -> orphan
    indexer = _ReconIndexer()
    rec = _build_reconciler(db, qdrant, indexer)

    report = await rec.reconcile_and_repair()

    assert qdrant.deleted == [[99]]
    assert report.orphans_deleted == 1


@pytest.mark.asyncio
async def test_reconciler_reindexes_missing_on_disk(tmp_path: Path) -> None:
    rows = [SimpleNamespace(id=5, mirror_path=str(tmp_path))]  # path exists
    mirror = _mirror(mirror_id=5)
    db = _ReconDb(rows, {5: mirror})
    qdrant = _ReconQdrant(indexed=set())  # 5 expected but not indexed -> missing
    indexer = _ReconIndexer()
    rec = _build_reconciler(db, qdrant, indexer)

    report = await rec.reconcile_and_repair()

    assert len(indexer.calls) == 1
    called_mirror, called_path, force = indexer.calls[0]
    assert called_mirror is mirror
    assert called_path == tmp_path
    assert force is True
    assert report.missing_reindexed == 1
    assert report.missing_cleared == 0


@pytest.mark.asyncio
async def test_reconciler_clears_missing_gone_from_disk() -> None:
    rows = [SimpleNamespace(id=7, mirror_path="/definitely/not/here")]
    db = _ReconDb(rows, {7: _mirror(mirror_id=7)})
    qdrant = _ReconQdrant(indexed=set())  # missing
    indexer = _ReconIndexer()
    rec = _build_reconciler(db, qdrant, indexer)

    report = await rec.reconcile_and_repair()

    assert indexer.calls == []  # cannot re-index without disk
    assert report.missing_cleared == 1
    # An UPDATE (clear) statement was executed.
    assert len(db.exec_log) >= 2  # the SELECT + the clear UPDATE


@pytest.mark.asyncio
async def test_reconciler_skips_when_qdrant_unavailable() -> None:
    db = _ReconDb([SimpleNamespace(id=1, mirror_path="/x")], {1: _mirror()})
    qdrant = _ReconQdrant(indexed={1}, available=False)
    indexer = _ReconIndexer()
    rec = _build_reconciler(db, qdrant, indexer)

    report = await rec.reconcile_and_repair()

    assert report.orphans_deleted == 0
    assert report.missing_reindexed == 0
    assert qdrant.deleted == []
    assert indexer.calls == []


# ---------------------------------------------------------------------------
# QdrantVectorStore methods (bypass __init__ to avoid a live client)
# ---------------------------------------------------------------------------


def _bare_store() -> Any:
    from app.infrastructure.vector.qdrant_store import QdrantVectorStore

    store = QdrantVectorStore.__new__(QdrantVectorStore)
    store._available = True
    store._required = False
    store._environment = "prod"
    store._user_scope = "owner"
    store._collection_name = "notes_test"
    return store


def test_delete_git_mirror_points_derives_point_ids() -> None:
    store = _bare_store()
    store._client = MagicMock()

    store.delete_git_mirror_points([1, 2])

    assert store._client.delete.call_count == 1
    _args, kwargs = store._client.delete.call_args
    selector = kwargs["points_selector"]
    expected = [
        git_mirror_point_id("prod", "owner", 1),
        git_mirror_point_id("prod", "owner", 2),
    ]
    assert list(selector.points) == expected


def test_delete_git_mirror_points_noop_on_empty() -> None:
    store = _bare_store()
    store._client = MagicMock()
    store.delete_git_mirror_points([])
    store._client.delete.assert_not_called()


def test_get_indexed_git_mirror_ids_parses_payload() -> None:
    store = _bare_store()
    records = [
        SimpleNamespace(payload={"mirror_id": 3}),
        SimpleNamespace(payload={"mirror_id": "4"}),  # coerced to int
        SimpleNamespace(payload={"mirror_id": None}),  # skipped
        SimpleNamespace(payload={}),  # skipped
    ]
    store._scroll_all = MagicMock(return_value=records)

    result = store.get_indexed_git_mirror_ids(limit=100)

    assert result == {3, 4}


# ---------------------------------------------------------------------------
# GitMirrorVectorIndexedEntityAdapter stat assembly (fake session)
# ---------------------------------------------------------------------------


class _AdapterSession:
    """Fake session: scalars() -> expected ids; scalar() -> queued values."""

    def __init__(self, expected: list[int], scalars_queue: list[Any]) -> None:
        self._expected = expected
        self._queue = list(scalars_queue)

    async def scalars(self, _stmt: Any) -> list[int]:
        return self._expected

    async def scalar(self, _stmt: Any) -> Any:
        return self._queue.pop(0)


@pytest.mark.asyncio
async def test_adapter_inspect_computes_missing_vectors() -> None:
    from app.infrastructure.vector.reconciliation import GitMirrorVectorIndexedEntityAdapter

    # expected ids {1,2,3}; queued scalar() returns: missing_embeddings, oldest, latest
    session = _AdapterSession([1, 2, 3], [4, None, None])
    store = MagicMock()
    store.get_indexed_git_mirror_ids = MagicMock(return_value={1, 2})  # 3 missing its vector

    stats = await GitMirrorVectorIndexedEntityAdapter().inspect(
        session,
        vector_store=store,
        vector_store_available=True,
        scan_limit=100,
        expected_model_version="1.0",
    )

    assert stats.entity_type == "git_mirror"
    assert stats.expected_ids == {1, 2, 3}
    assert stats.indexed_ids == {1, 2}
    assert stats.missing_vectors == 1  # {1,2,3} - {1,2}
    assert stats.missing_embeddings == 4
    assert stats.stale_model_count == 0
