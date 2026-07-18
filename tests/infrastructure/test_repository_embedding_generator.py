"""Focused tests for repository embedding persistence."""

from __future__ import annotations

import datetime as dt
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.db.models.repository import RepoSource
from app.infrastructure.embedding.repository_embedding import (
    RepositoryEmbeddingBatchItem,
    RepositoryEmbeddingGenerator,
)
from app.infrastructure.vector.point_ids import repository_point_id


def _make_repo(repo_id: int, *, full_name: str | None = None) -> MagicMock:
    repo = MagicMock()
    repo.id = repo_id
    repo.user_id = 42
    repo.github_id = 10_000 + repo_id
    repo.full_name = full_name or f"owner/repo-{repo_id}"
    repo.description = f"Repository {repo_id}"
    repo.primary_language = "Python"
    repo.languages_json = {"Python": 100}
    repo.topics_json = ["testing", "embeddings"]
    repo.readme_excerpt = None
    repo.source = RepoSource.MANUAL
    repo.is_starred = False
    repo.created_at = dt.datetime(2026, 1, repo_id, tzinfo=dt.UTC)
    return repo


def _make_transaction_db(returned_rows: list[MagicMock]) -> tuple[MagicMock, list[Any]]:
    executed_statements: list[Any] = []

    result = MagicMock()
    result.scalars.return_value.all.return_value = returned_rows
    result.scalar_one.side_effect = returned_rows

    mock_session = AsyncMock()

    async def execute(stmt):
        executed_statements.append(stmt)
        return result

    mock_session.execute = AsyncMock(side_effect=execute)

    transaction_ctx = MagicMock()

    async def _aenter(self):
        return mock_session

    async def _aexit(self, *args):
        pass

    transaction_ctx.__aenter__ = _aenter
    transaction_ctx.__aexit__ = _aexit

    db = MagicMock()
    db.transaction.return_value = transaction_ctx
    return db, executed_statements


class FakeEmbeddingService:
    def __init__(self) -> None:
        self.generate_embedding = AsyncMock()
        self.generate_embeddings_batch = AsyncMock()

    def serialize_embedding(self, embedding) -> bytes:
        return bytes(str(list(embedding)), "utf-8")

    def get_model_name(self, language=None) -> str:
        return "test-model"

    def get_dimensions(self, language=None) -> int:
        return 2

    async def get_dimensions_async(self, language=None) -> int:
        return 2


@pytest.mark.asyncio
async def test_upsert_db_row_returns_insert_returning_row_without_extra_read() -> None:
    returned_row = MagicMock()
    returned_row.id = 123

    result = MagicMock()
    result.scalar_one.return_value = returned_row

    executed_statements = []
    mock_session = AsyncMock()

    async def execute(stmt):
        executed_statements.append(stmt)
        return result

    mock_session.execute = AsyncMock(side_effect=execute)

    transaction_ctx = MagicMock()

    async def _aenter(self):
        return mock_session

    async def _aexit(self, *args):
        pass

    transaction_ctx.__aenter__ = _aenter
    transaction_ctx.__aexit__ = _aexit

    db = MagicMock()
    db.transaction.return_value = transaction_ctx

    generator = RepositoryEmbeddingGenerator(
        embedding_service=MagicMock(),
        qdrant_store=None,
        db=db,
        environment="test",
        user_scope="default",
    )

    actual = await generator._upsert_db_row(
        repository_id=7,
        model_name="model",
        model_version="1.0",
        embedding_blob=b"blob",
        dimensions=3,
        language=None,
    )

    assert actual is returned_row
    db.transaction.assert_called_once_with()
    db.session.assert_not_called()

    compiled = str(
        executed_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "ON CONFLICT" in compiled
    assert "RETURNING repository_embeddings.id" in compiled
    assert "repository_embeddings.repository_id" in compiled


@pytest.mark.asyncio
@pytest.mark.parametrize(("qdrant_ack", "expected_transactions"), [(True, 2), (False, 1)])
async def test_regenerate_batch_marks_indexed_only_after_qdrant_ack(
    qdrant_ack: bool,
    expected_transactions: int,
) -> None:
    repos = [_make_repo(1), _make_repo(2)]
    returned_rows = []
    for repo in repos:
        row = MagicMock()
        row.repository_id = repo.id
        returned_rows.append(row)

    db, executed_statements = _make_transaction_db(returned_rows)
    embedding_service = FakeEmbeddingService()
    embedding_service.generate_embeddings_batch.return_value = [[0.1, 0.2], [0.3, 0.4]]

    qdrant = MagicMock()
    qdrant.available = True
    qdrant.upsert_notes.return_value = qdrant_ack

    generator = RepositoryEmbeddingGenerator(
        embedding_service=cast("Any", embedding_service),
        qdrant_store=qdrant,
        db=db,
        environment="test",
        user_scope="default",
    )

    result = await generator.regenerate_batch(
        [
            RepositoryEmbeddingBatchItem(
                repository=repos[0],
                analysis=None,
                correlation_id="correlation-1",
            ),
            RepositoryEmbeddingBatchItem(
                repository=repos[1],
                analysis=None,
                correlation_id="correlation-2",
            ),
        ]
    )

    assert [success.repository_id for success in result.successes] == [1, 2]
    assert result.failures == []
    embedding_service.generate_embeddings_batch.assert_awaited_once()
    embedding_service.generate_embedding.assert_not_awaited()
    assert db.transaction.call_count == expected_transactions
    assert len(executed_statements) == expected_transactions
    qdrant.upsert_notes.assert_called_once()

    vectors, metadatas, point_ids = qdrant.upsert_notes.call_args.args
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert [metadata["repository_id"] for metadata in metadatas] == [1, 2]
    assert [metadata["entity_type"] for metadata in metadatas] == [
        "repository",
        "repository",
    ]
    assert len(point_ids) == 2

    compiled = str(
        executed_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "ON CONFLICT" in compiled
    assert "RETURNING repository_embeddings.id" in compiled
    assert "index_status" in compiled


@pytest.mark.asyncio
async def test_regenerate_batch_falls_back_to_single_rows_on_batch_failure() -> None:
    repos = [_make_repo(1), _make_repo(2)]
    returned_row = MagicMock()
    returned_row.repository_id = 1

    db, executed_statements = _make_transaction_db([returned_row])
    embedding_service = FakeEmbeddingService()
    embedding_service.generate_embeddings_batch.side_effect = RuntimeError("batch failed")
    embedding_service.generate_embedding.side_effect = [[0.1, 0.2], RuntimeError("row failed")]

    generator = RepositoryEmbeddingGenerator(
        embedding_service=cast("Any", embedding_service),
        qdrant_store=None,
        db=db,
        environment="test",
        user_scope="default",
    )

    result = await generator.regenerate_batch(
        [
            RepositoryEmbeddingBatchItem(
                repository=repos[0],
                analysis=None,
                correlation_id="correlation-1",
            ),
            RepositoryEmbeddingBatchItem(
                repository=repos[1],
                analysis=None,
                correlation_id="correlation-2",
            ),
        ]
    )

    assert [success.repository_id for success in result.successes] == [1]
    assert [failure.repository_id for failure in result.failures] == [2]
    embedding_service.generate_embeddings_batch.assert_awaited_once()
    assert embedding_service.generate_embedding.await_count == 2
    assert len(executed_statements) == 1


@pytest.mark.asyncio
async def test_delete_repository_point_uses_repository_point_id() -> None:
    qdrant = MagicMock()
    qdrant.available = True
    qdrant._client = MagicMock()
    qdrant._client.delete = MagicMock()
    qdrant._collection_name = "embeddings"
    generator = RepositoryEmbeddingGenerator(
        embedding_service=MagicMock(),
        qdrant_store=qdrant,
        db=MagicMock(),
        environment="test",
        user_scope="default",
    )

    await generator.delete_repository_point(123)

    qdrant._client.delete.assert_called_once()
    kwargs = qdrant._client.delete.call_args.kwargs
    assert kwargs["collection_name"] == "embeddings"
    assert kwargs["points_selector"].points == [repository_point_id("test", "default", 123)]
    assert kwargs["wait"] is True
