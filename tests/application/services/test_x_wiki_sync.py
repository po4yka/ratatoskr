"""Unit tests for XWikiSyncService."""

from __future__ import annotations

import hashlib
import logging
import pathlib
import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from app.application.services.x_wiki_sync import (
    WikiSyncSummary,
    XWikiSyncService,
    x_wiki_point_id,
)
from app.infrastructure.vector.point_ids import str_to_uuid

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass
class _FakeVectorStore:
    """In-memory stub honoring the wiki-sync subset of QdrantVectorStore."""

    environment: str = "test"
    user_scope: str = "unit"
    indexed: dict[str, str] = field(default_factory=dict)
    upsert_calls: list[dict[str, object]] = field(default_factory=list)
    delete_calls: list[list[str]] = field(default_factory=list)

    def get_indexed_x_wiki_path_hashes(
        self, *, user_id: int | None = None, limit: int | None = 5000
    ) -> dict[str, str]:
        del user_id, limit
        return dict(self.indexed)

    def upsert_notes(
        self,
        vectors: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, object]],
        ids: Sequence[str] | None = None,
    ) -> None:
        self.upsert_calls.append(
            {
                "vectors": [list(v) for v in vectors],
                "metadatas": [dict(m) for m in metadatas],
                "ids": list(ids) if ids is not None else None,
            }
        )
        if ids is None:
            return
        for raw_id, metadata in zip(ids, metadatas, strict=True):
            content_hash = metadata.get("content_hash")
            if isinstance(content_hash, str):
                self.indexed[raw_id] = content_hash

    def delete_x_wiki_paths(self, wiki_paths: Sequence[str]) -> None:
        paths = list(wiki_paths)
        self.delete_calls.append(paths)
        for p in paths:
            self.indexed.pop(p, None)


class _FakeEmbeddingService:
    """Records embedding generation calls and returns a deterministic vector.

    Honors the ``EmbeddingProviderPort`` protocol so mypy accepts it as a
    drop-in collaborator in ``XWikiSyncService.__init__``.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def generate_embedding(
        self,
        text: str,
        *,
        language: str | None = None,
        task_type: str = "document",
    ) -> list[float]:
        self.calls.append({"text": text, "language": language, "task_type": task_type})
        return [0.1, 0.2, 0.3]

    async def generate_embeddings_batch(
        self,
        texts: Sequence[str],
        *,
        language: str | None = None,
        task_type: str = "document",
    ) -> list[list[float]]:
        return [
            await self.generate_embedding(text, language=language, task_type=task_type)
            for text in texts
        ]

    def serialize_embedding(self, embedding: list[float]) -> bytes:
        return struct.pack(f"<{len(embedding)}f", *embedding)

    def deserialize_embedding(self, blob: bytes) -> list[float]:
        return list(struct.unpack(f"<{len(blob) // 4}f", blob))

    def get_model_name(self, language: str | None = None) -> str:
        del language
        return "fake-embedding-model"


def _write_md(directory: pathlib.Path, name: str, body: str) -> pathlib.Path:
    """Create a markdown file with `body`; return its absolute path."""
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path.resolve()


def test_deterministic_point_id_same_path_yields_same_uuid(tmp_path: pathlib.Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    path = _write_md(library, "alpha.md", "hello world")

    first = x_wiki_point_id(path)
    second = x_wiki_point_id(path)

    assert first == second
    assert first == str_to_uuid(str(path.absolute()))


@pytest.mark.asyncio
async def test_skip_on_unchanged_hash(tmp_path: pathlib.Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    body = "stable body"
    path = _write_md(library, "alpha.md", body)
    expected_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    vector_store = _FakeVectorStore(indexed={str(path): expected_hash})
    embedding = _FakeEmbeddingService()
    service = XWikiSyncService(
        library_path=library,
        vector_store=vector_store,
        embedding_service=embedding,
    )

    summary = await service.sync()

    assert summary == WikiSyncSummary(
        files_seen=1, files_changed=0, files_skipped=1, orphans_deleted=0
    )
    assert embedding.calls == []
    assert vector_store.upsert_calls == []
    assert vector_store.delete_calls == []


@pytest.mark.asyncio
async def test_re_embed_on_hash_drift(tmp_path: pathlib.Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    path = _write_md(library, "alpha.md", "fresh body")
    fresh_hash = hashlib.sha256(b"fresh body").hexdigest()

    vector_store = _FakeVectorStore(indexed={str(path): "stale-hash"})
    embedding = _FakeEmbeddingService()
    service = XWikiSyncService(
        library_path=library,
        vector_store=vector_store,
        embedding_service=embedding,
    )

    summary = await service.sync()

    assert summary == WikiSyncSummary(
        files_seen=1, files_changed=1, files_skipped=0, orphans_deleted=0
    )
    assert len(embedding.calls) == 1
    assert embedding.calls[0]["text"] == "fresh body"
    assert len(vector_store.upsert_calls) == 1
    call = vector_store.upsert_calls[0]
    assert call["ids"] == [str(path)]
    metadatas = call["metadatas"]
    assert isinstance(metadatas, list)
    metadata = metadatas[0]
    assert metadata["entity_type"] == "x_wiki"
    assert metadata["wiki_path"] == str(path)
    assert metadata["content_hash"] == fresh_hash


@pytest.mark.asyncio
async def test_re_embed_on_new_file(tmp_path: pathlib.Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    path = _write_md(library, "alpha.md", "brand new body")

    vector_store = _FakeVectorStore()
    embedding = _FakeEmbeddingService()
    service = XWikiSyncService(
        library_path=library,
        vector_store=vector_store,
        embedding_service=embedding,
    )

    summary = await service.sync()

    assert summary == WikiSyncSummary(
        files_seen=1, files_changed=1, files_skipped=0, orphans_deleted=0
    )
    assert len(embedding.calls) == 1
    assert len(vector_store.upsert_calls) == 1


@pytest.mark.asyncio
async def test_orphan_delete(tmp_path: pathlib.Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    # No on-disk file; only an entry in the index points to a missing path.
    missing_path = str((library / "ghost.md").resolve())
    vector_store = _FakeVectorStore(indexed={missing_path: "doesnt-matter"})
    embedding = _FakeEmbeddingService()
    service = XWikiSyncService(
        library_path=library,
        vector_store=vector_store,
        embedding_service=embedding,
    )

    summary = await service.sync()

    assert summary == WikiSyncSummary(
        files_seen=0, files_changed=0, files_skipped=0, orphans_deleted=1
    )
    assert embedding.calls == []
    assert vector_store.delete_calls == [[missing_path]]


@pytest.mark.asyncio
async def test_graceful_missing_library_dir(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing = tmp_path / "does-not-exist"
    vector_store = _FakeVectorStore()
    embedding = _FakeEmbeddingService()
    service = XWikiSyncService(
        library_path=missing,
        vector_store=vector_store,
        embedding_service=embedding,
    )

    with caplog.at_level(logging.WARNING):
        summary = await service.sync()

    assert summary == WikiSyncSummary(0, 0, 0, 0)
    assert embedding.calls == []
    assert vector_store.upsert_calls == []
    assert vector_store.delete_calls == []
    assert any(record.message == "x_wiki_sync_library_missing" for record in caplog.records)


@pytest.mark.asyncio
async def test_oserror_on_read_is_logged_and_skipped(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An OSError on ``path.read_text`` skips that file and logs a warning."""
    library = tmp_path / "library"
    library.mkdir()
    path = _write_md(library, "unreadable.md", "body")

    vector_store = _FakeVectorStore()
    embedding = _FakeEmbeddingService()
    service = XWikiSyncService(
        library_path=library,
        vector_store=vector_store,
        embedding_service=embedding,
    )

    import unittest.mock as _mock

    with _mock.patch.object(pathlib.Path, "read_text", side_effect=OSError("permission denied")):
        with caplog.at_level(logging.WARNING):
            summary = await service.sync()

    # File was seen but not changed (read failed, so upsert never called).
    assert summary.files_seen == 1
    assert summary.files_changed == 0
    assert embedding.calls == []
    assert vector_store.upsert_calls == []
    assert any(record.message == "x_wiki_sync_read_failed" for record in caplog.records)


@pytest.mark.asyncio
async def test_user_id_forwarded_to_vector_store(tmp_path: pathlib.Path) -> None:
    """``user_id`` passed to the service is forwarded to ``get_indexed_x_wiki_path_hashes``."""
    library = tmp_path / "library"
    library.mkdir()

    recorded_user_ids: list[int | None] = []

    class _TrackingStore(_FakeVectorStore):
        def get_indexed_x_wiki_path_hashes(
            self, *, user_id: int | None = None, limit: int | None = 5000
        ) -> dict[str, str]:
            recorded_user_ids.append(user_id)
            return {}

    service = XWikiSyncService(
        library_path=library,
        vector_store=_TrackingStore(),
        embedding_service=_FakeEmbeddingService(),
        user_id=99,
    )

    await service.sync()

    assert recorded_user_ids == [99]


@pytest.mark.asyncio
async def test_mixed_pass_skips_changes_and_orphans(tmp_path: pathlib.Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    stable = _write_md(library, "stable.md", "stable body")
    drifted = _write_md(library, "drifted.md", "new body")
    _write_md(library, "fresh.md", "brand new")
    orphan_path = str((library / "missing.md").resolve())

    stable_hash = hashlib.sha256(b"stable body").hexdigest()
    vector_store = _FakeVectorStore(
        indexed={
            str(stable): stable_hash,
            str(drifted): "old-hash",
            orphan_path: "doesnt-matter",
        }
    )
    embedding = _FakeEmbeddingService()
    service = XWikiSyncService(
        library_path=library,
        vector_store=vector_store,
        embedding_service=embedding,
    )

    summary = await service.sync()

    # stable -> skipped, drifted + fresh -> changed (2), missing -> orphan
    assert summary.files_seen == 3
    assert summary.files_changed == 2
    assert summary.files_skipped == 1
    assert summary.orphans_deleted == 1
    assert len(embedding.calls) == 2
    assert vector_store.delete_calls == [[orphan_path]]
