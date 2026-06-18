"""Regression: dedupe ON CONFLICT must not mutate the winning request row."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.dialects import postgresql

from app.domain.models.request import RequestStatus
from app.infrastructure.persistence.repositories.request_repository import (
    RequestRepositoryAdapter,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _CapturingSession:
    """Minimal async session that records the statement handed to ``scalar``."""

    def __init__(self) -> None:
        self.statements: list[Any] = []
        self._calls = 0

    async def scalar(self, statement: Any) -> int | None:
        self.statements.append(statement)
        self._calls += 1
        return 123 if self._calls == 1 else None


class _CapturingDatabase:
    """Stands in for :class:`Database`, yielding a capturing session."""

    def __init__(self) -> None:
        self.session = _CapturingSession()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_CapturingSession]:
        yield self.session


def _compiled_sql(statement: Any) -> str:
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in sql, sql
    return sql


@pytest.mark.asyncio
async def test_dedupe_hash_conflict_does_not_update_existing_request() -> None:
    """A dedupe_hash conflict must be idempotent instead of overwriting fields."""
    database = _CapturingDatabase()
    repo = RequestRepositoryAdapter(database)  # type: ignore[arg-type]

    await repo.async_create_request(
        type_="url",
        status=RequestStatus.PENDING,
        correlation_id="cid-other-identity",
        user_id=999,
        chat_id=1,
        dedupe_hash="shared-hash",
        input_url="https://example.com/shared",
    )

    sql = _compiled_sql(database.session.statements[0])
    assert "ON CONFLICT (dedupe_hash) DO NOTHING" in sql
    assert "DO UPDATE SET" not in sql


@pytest.mark.asyncio
async def test_paper_canonical_conflict_does_not_update_existing_request() -> None:
    """A paper_canonical_id conflict must also be idempotent."""
    database = _CapturingDatabase()
    repo = RequestRepositoryAdapter(database)  # type: ignore[arg-type]

    await repo.async_create_request(
        type_="url",
        status=RequestStatus.PENDING,
        correlation_id="cid-other-identity",
        user_id=999,
        chat_id=1,
        paper_canonical_id="arxiv:1234.5678",
        input_url="https://arxiv.org/abs/1234.5678",
    )

    sql = _compiled_sql(database.session.statements[0])
    assert "ON CONFLICT (paper_canonical_id) DO NOTHING" in sql
    assert "DO UPDATE SET" not in sql
