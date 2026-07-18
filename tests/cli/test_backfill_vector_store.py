from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cli import backfill_vector_store
from app.config import QdrantConfig


def _qdrant_config() -> QdrantConfig:
    return QdrantConfig(
        url="http://qdrant.test",
        api_key=None,
        environment="test",
        user_scope="owner",
        collection_version="v1",
        required=False,
        connection_timeout=1.0,
    )


def _app_config() -> SimpleNamespace:
    return SimpleNamespace(
        embedding=SimpleNamespace(
            provider="gemini",
            gemini_model="test-model",
            gemini_dimensions=1,
            embedding_dim=1,
            max_token_length=1024,
        )
    )


def _patch_backfill_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    summaries: list[dict],
    embedding_repo: object,
    embedding_service: object,
    vector_store: object,
    generator: object | None = None,
) -> MagicMock:
    fake_db = MagicMock()
    fake_db.dispose = AsyncMock()

    monkeypatch.setattr(backfill_vector_store, "load_config", lambda **_kwargs: _app_config())
    monkeypatch.setattr(backfill_vector_store, "DatabaseConfig", lambda dsn=None: MagicMock())
    monkeypatch.setattr(backfill_vector_store, "Database", lambda config: fake_db)
    monkeypatch.setattr(
        backfill_vector_store,
        "EmbeddingRepositoryAdapter",
        lambda _db: embedding_repo,
    )
    monkeypatch.setattr(
        backfill_vector_store,
        "create_embedding_service",
        lambda _cfg: embedding_service,
    )
    monkeypatch.setattr(
        backfill_vector_store,
        "QdrantVectorStore",
        lambda **_kwargs: vector_store,
    )
    monkeypatch.setattr(
        backfill_vector_store,
        "_fetch_summaries_page",
        AsyncMock(side_effect=[summaries, []]),
    )
    monkeypatch.setattr(
        backfill_vector_store,
        "SummaryEmbeddingGenerator",
        lambda **_kwargs: (
            generator
            or SimpleNamespace(generate_embedding_for_summary=AsyncMock(return_value=True))
        ),
    )
    return fake_db


def test_main_returns_zero_for_help(monkeypatch, capsys) -> None:
    monkeypatch.setattr(backfill_vector_store.sys, "argv", ["backfill_vector_store.py", "--help"])

    assert backfill_vector_store.main() == 0
    assert "--dsn=DSN" in capsys.readouterr().out


def test_main_rejects_legacy_db_option(monkeypatch) -> None:
    monkeypatch.setattr(
        backfill_vector_store.sys,
        "argv",
        ["backfill_vector_store.py", "--db=/tmp/ratatoskr.db"],
    )

    assert backfill_vector_store.main() == 1


@pytest.mark.asyncio
async def test_backfill_vector_store_batches_chunk_window_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEmbeddingService:
        def __init__(self) -> None:
            self.batch_calls: list[tuple[list[str], str | None, str | None]] = []

        async def generate_embeddings_batch(
            self,
            texts,
            *,
            language=None,
            task_type=None,
        ):
            self.batch_calls.append((list(texts), language, task_type))
            vectors = {
                "first en": [1.0],
                "ru": [2.0],
                "second en": [3.0],
            }
            return [vectors[text] for text in texts]

        def deserialize_embedding(self, _blob):
            raise AssertionError("chunk-window backfill must not use summary embedding blob")

    class FakeVectorStore:
        def __init__(self) -> None:
            self.replaced: list[tuple[int, list[list[float]], list[dict]]] = []
            self.deleted: list[int] = []

        def replace_request_notes(self, request_id, vectors, metadata, *, wait=True) -> bool:
            self.replaced.append((request_id, vectors, metadata))
            return True

        def delete_by_request_id(self, request_id) -> None:
            self.deleted.append(request_id)

    summaries = [
        {
            "id": 101,
            "request_id": 201,
            "lang": "en",
            "json_payload": {
                "summary_250": "summary",
                "semantic_chunks": [
                    {"text": "first en", "language": "en"},
                    {"text": "ru", "language": "ru"},
                    {"text": "second en", "language": "en"},
                ],
            },
            "request": {"user_id": 301},
        }
    ]
    embedding_repo = SimpleNamespace(
        async_get_summary_embeddings=AsyncMock(
            return_value=[{"summary_id": 101, "embedding_blob": b"exists"}]
        ),
        async_mark_summary_embeddings_indexed=AsyncMock(return_value=[101]),
        async_get_summary_embedding=AsyncMock(
            side_effect=AssertionError("backfill should use bulk embedding lookup")
        ),
    )
    embedding_service = FakeEmbeddingService()
    vector_store = FakeVectorStore()
    generator = SimpleNamespace(generate_embedding_for_summary=AsyncMock(return_value=True))

    fake_db = _patch_backfill_dependencies(
        monkeypatch,
        summaries=summaries,
        embedding_repo=embedding_repo,
        embedding_service=embedding_service,
        vector_store=vector_store,
        generator=generator,
    )

    await backfill_vector_store.backfill_vector_store(
        None,
        _qdrant_config(),
        batch_size=10,
    )

    embedding_repo.async_get_summary_embeddings.assert_awaited_once_with([101])
    generator.generate_embedding_for_summary.assert_not_awaited()
    assert embedding_service.batch_calls == [
        (["first en", "second en"], "en", "document"),
        (["ru"], "ru", "document"),
    ]
    assert vector_store.deleted == []
    assert len(vector_store.replaced) == 1
    embedding_repo.async_mark_summary_embeddings_indexed.assert_awaited_once_with({101: None})
    request_id, vectors, metadata = vector_store.replaced[0]
    assert request_id == 201
    assert vectors == [[1.0], [2.0], [3.0]]
    assert [item["window_index"] for item in metadata] == [0, 1, 2]
    fake_db.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_vector_store_refetches_generated_embeddings_in_bulk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEmbeddingService:
        async def generate_embeddings_batch(self, *_args, **_kwargs):
            raise AssertionError("summary without chunks should reuse stored summary embedding")

        def deserialize_embedding(self, blob):
            assert blob == b"new"
            return [9.0]

    class FakeVectorStore:
        def __init__(self) -> None:
            self.replaced: list[tuple[int, list[list[float]], list[dict]]] = []

        def replace_request_notes(self, request_id, vectors, metadata, *, wait=True) -> bool:
            self.replaced.append((request_id, vectors, metadata))
            return True

        def delete_by_request_id(self, _request_id) -> None:
            raise AssertionError("summary with text should not be deleted")

    summaries = [
        {
            "id": 101,
            "request_id": 201,
            "lang": "en",
            "json_payload": {"summary_250": "summary"},
            "request": {"user_id": 301},
        }
    ]
    embedding_repo = SimpleNamespace(
        async_get_summary_embeddings=AsyncMock(
            side_effect=[
                [],
                [{"summary_id": 101, "embedding_blob": b"new"}],
            ]
        ),
        async_mark_summary_embeddings_indexed=AsyncMock(return_value=[101]),
        async_get_summary_embedding=AsyncMock(
            side_effect=AssertionError("backfill should refetch generated embeddings in bulk")
        ),
    )
    generator = SimpleNamespace(generate_embedding_for_summary=AsyncMock(return_value=True))
    vector_store = FakeVectorStore()

    _patch_backfill_dependencies(
        monkeypatch,
        summaries=summaries,
        embedding_repo=embedding_repo,
        embedding_service=FakeEmbeddingService(),
        vector_store=vector_store,
        generator=generator,
    )

    await backfill_vector_store.backfill_vector_store(
        None,
        _qdrant_config(),
        batch_size=10,
    )

    assert embedding_repo.async_get_summary_embeddings.await_args_list == [
        (([101],), {}),
        (([101],), {}),
    ]
    embedding_repo.async_mark_summary_embeddings_indexed.assert_awaited_once_with({101: None})
    generator.generate_embedding_for_summary.assert_awaited_once_with(
        summary_id=101,
        payload={"summary_250": "summary"},
        language="en",
        force=False,
    )
    assert [(request_id, vectors) for request_id, vectors, _metadata in vector_store.replaced] == [
        (201, [[9.0]])
    ]
