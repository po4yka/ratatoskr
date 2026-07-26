"""Regression tests for owner-role preservation during user metadata upserts."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.dialects import postgresql

from app.infrastructure.persistence.repositories.user_repository import (
    UserRepositoryAdapter,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _CapturingSession:
    def __init__(self) -> None:
        self.statement: Any | None = None

    async def execute(self, statement: Any) -> None:
        self.statement = statement


class _CapturingDatabase:
    def __init__(self) -> None:
        self.session = _CapturingSession()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_CapturingSession]:
        yield self.session


def _conflict_update_sql(statement: Any) -> str:
    sql = str(statement.compile(dialect=postgresql.dialect()))
    _, update_sql = sql.split("DO UPDATE SET", maxsplit=1)
    return update_sql


@pytest.mark.asyncio
async def test_passive_user_upsert_does_not_change_owner_role() -> None:
    database = _CapturingDatabase()
    repository = UserRepositoryAdapter(database)  # type: ignore[arg-type]

    await repository.async_upsert_user(telegram_user_id=101, username="owner")

    assert database.session.statement is not None
    update_sql = _conflict_update_sql(database.session.statement)
    assert "username =" in update_sql
    assert "updated_at =" in update_sql
    assert "is_owner =" not in update_sql


@pytest.mark.asyncio
async def test_explicit_user_upsert_can_change_owner_role() -> None:
    database = _CapturingDatabase()
    repository = UserRepositoryAdapter(database)  # type: ignore[arg-type]

    await repository.async_upsert_user(
        telegram_user_id=101,
        username="former-owner",
        is_owner=False,
    )

    assert database.session.statement is not None
    update_sql = _conflict_update_sql(database.session.statement)
    assert "is_owner =" in update_sql
