from __future__ import annotations

import datetime as dt
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.dto.repository import RepositoryDetailDTO
from app.application.services.repository_service import (
    RepositoryService,
    RepositoryServiceNotFoundError,
)
from app.db.session import Database
from app.infrastructure.persistence.repositories.repository_read_repository import (
    RepositoryReadRepositoryAdapter,
)


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class _Session:
    def __init__(self, row: Any) -> None:
        self._row = row
        self.statement = None
        self.statements: list[Any] = []

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def execute(self, statement: Any) -> _Result:
        self.statement = statement
        self.statements.append(statement)
        return _Result(self._row)


class _Database:
    def __init__(self, row: Any = None) -> None:
        self.session_ctx = _Session(row)
        self.transaction_started = False

    def session(self) -> _Session:
        return self.session_ctx

    def transaction(self) -> _Session:
        self.transaction_started = True
        return self.session_ctx


class _RepositoryPort:
    def __init__(self, repository: RepositoryDetailDTO | None) -> None:
        self.repository = repository
        self.deleted: tuple[int, int] | None = None

    async def list_repositories(self, **_: Any) -> Any:
        raise NotImplementedError

    async def get_owned_repository(
        self,
        *,
        repository_id: int,
        user_id: int,
    ) -> RepositoryDetailDTO | None:
        _ = repository_id, user_id
        return self.repository

    async def delete_owned_repository(self, *, repository_id: int, user_id: int) -> None:
        self.deleted = (repository_id, user_id)


def _repository_detail(repository_id: int) -> RepositoryDetailDTO:
    return RepositoryDetailDTO(
        id=repository_id,
        github_id=10_000 + repository_id,
        full_name="owner/repo",
        owner="owner",
        name="repo",
        description=None,
        primary_language="Python",
        topics=[],
        stars=0,
        forks=0,
        is_starred=False,
        is_archived=False,
        pushed_at=None,
        last_synced_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        pending_analysis=False,
        has_analysis=False,
        source="manual",
        homepage_url=None,
        license_spdx=None,
        is_fork=False,
        is_template=False,
        languages={},
        readme_excerpt=None,
        analysis=None,
        analysis_model=None,
        analysis_at=None,
        content_hash=None,
        created_at_github=None,
        watchers=0,
    )


async def test_load_owned_repository_filters_by_repository_and_user_id() -> None:
    db = _Database(row=object())
    adapter = RepositoryReadRepositoryAdapter(cast("Database", db))

    row = await adapter.load_owned_repository(repository_id=123, user_id=456)

    assert row is not None
    assert db.session_ctx.statement is not None
    compiled = str(db.session_ctx.statement.compile(compile_kwargs={"literal_binds": True}))
    assert "repositories.id = 123" in compiled
    assert "repositories.user_id = 456" in compiled


async def test_reanalyze_denies_cross_user_repository_before_use_case() -> None:
    use_case = MagicMock()
    use_case.analyze = AsyncMock()
    service = RepositoryService(repository_repo=_RepositoryPort(repository=None))

    with pytest.raises(RepositoryServiceNotFoundError):
        await service.reanalyze_repository(
            repository_id=123,
            user_id=456,
            use_case=use_case,
            correlation_id="cid",
        )

    use_case.analyze.assert_not_awaited()


async def test_get_repository_denies_cross_user_repository() -> None:
    service = RepositoryService(repository_repo=_RepositoryPort(repository=None))

    with pytest.raises(RepositoryServiceNotFoundError):
        await service.get_repository(repository_id=123, user_id=456)


async def test_delete_repository_denies_cross_user_repository_before_delete() -> None:
    repository_port = _RepositoryPort(repository=None)
    embedding_gen = MagicMock()
    embedding_gen.delete_repository_point = AsyncMock()
    service = RepositoryService(repository_repo=repository_port, embedding_gen=embedding_gen)

    with pytest.raises(RepositoryServiceNotFoundError):
        await service.delete_repository(repository_id=123, user_id=456)

    assert repository_port.deleted is None
    embedding_gen.delete_repository_point.assert_not_awaited()


async def test_delete_repository_deletes_embedding_point_before_self_scoped_db_delete() -> None:
    repository_port = _RepositoryPort(repository=_repository_detail(123))
    embedding_gen = MagicMock()
    embedding_gen.delete_repository_point = AsyncMock()
    service = RepositoryService(repository_repo=repository_port, embedding_gen=embedding_gen)

    await service.delete_repository(repository_id=123, user_id=456)

    embedding_gen.delete_repository_point.assert_awaited_once_with(123)
    assert repository_port.deleted == (123, 456)


async def test_repository_adapter_delete_is_self_scoped() -> None:
    db = _Database(row=object())
    adapter = RepositoryReadRepositoryAdapter(cast("Database", db))

    await adapter.delete_owned_repository(repository_id=123, user_id=456)

    assert db.transaction_started is True
    assert len(db.session_ctx.statements) == 2
    compiled_delete = str(
        db.session_ctx.statements[-1].compile(compile_kwargs={"literal_binds": True})
    )
    assert "DELETE FROM repositories" in compiled_delete
    assert "repositories.id = 123" in compiled_delete
    assert "repositories.user_id = 456" in compiled_delete
