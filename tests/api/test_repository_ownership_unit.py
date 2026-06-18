from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.routers.repositories import (
    _load_owned_repository,
    delete_repository,
    get_repository,
    reanalyze_repository,
)
from app.db.session import Database


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class _Session:
    def __init__(self, row: Any) -> None:
        self._row = row
        self.statement = None

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def execute(self, statement: Any) -> _Result:
        self.statement = statement
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


async def test_load_owned_repository_filters_by_repository_and_user_id() -> None:
    db = _Database(row=object())

    row = await _load_owned_repository(cast("Database", db), repository_id=123, user_id=456)

    assert row is not None
    assert db.session_ctx.statement is not None
    compiled = str(db.session_ctx.statement.compile(compile_kwargs={"literal_binds": True}))
    assert "repositories.id = 123" in compiled
    assert "repositories.user_id = 456" in compiled


async def test_reanalyze_denies_cross_user_repository_before_use_case() -> None:
    use_case = MagicMock()
    use_case.analyze = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await reanalyze_repository(
            repository_id=123,
            user={"user_id": 456},
            use_case=use_case,
            correlation_id="cid",
            db=cast("Database", _Database(row=None)),
        )

    assert exc_info.value.status_code == 404
    use_case.analyze.assert_not_awaited()


async def test_get_repository_denies_cross_user_repository() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await get_repository(
            repository_id=123,
            user={"user_id": 456},
            db=cast("Database", _Database(row=None)),
        )

    assert exc_info.value.status_code == 404


async def test_delete_repository_denies_cross_user_repository_before_delete() -> None:
    db = _Database(row=None)

    with pytest.raises(HTTPException) as exc_info:
        await delete_repository(
            repository_id=123,
            user={"user_id": 456},
            db=cast("Database", db),
            qdrant=None,
        )

    assert exc_info.value.status_code == 404
    assert db.transaction_started is False
