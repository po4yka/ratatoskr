"""Regression: dedupe ON CONFLICT must not overwrite create-time ownership.

A repeat of the same URL (same ``dedupe_hash``) or academic paper
(``paper_canonical_id``) coming from a *different* identity must never
rewrite ``requests.user_id``. Overwriting it would be a forward-looking IDOR /
ownership-transfer bug. These tests inspect the SQL that
``async_create_request`` actually emits, so they run without a live Postgres.
"""

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

    async def scalar(self, statement: Any) -> int:
        self.statements.append(statement)
        return 123


class _CapturingDatabase:
    """Stands in for :class:`Database`, yielding a capturing session."""

    def __init__(self) -> None:
        self.session = _CapturingSession()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_CapturingSession]:
        yield self.session


def _compiled_set_clause(statement: Any) -> str:
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "DO UPDATE SET" in sql, sql
    return sql.split("DO UPDATE SET", 1)[1]


@pytest.mark.asyncio
async def test_dedupe_hash_conflict_does_not_overwrite_user_id() -> None:
    """A dedupe_hash conflict must exclude user_id from the ON CONFLICT SET."""
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

    set_clause = _compiled_set_clause(database.session.statements[0])
    assert "user_id =" not in set_clause
    # Ownership is the only excluded mutable field; other fields still update.
    assert "correlation_id =" in set_clause
    assert "status =" in set_clause


@pytest.mark.asyncio
async def test_paper_canonical_conflict_does_not_overwrite_user_id() -> None:
    """A paper_canonical_id conflict must also exclude user_id from the SET."""
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

    set_clause = _compiled_set_clause(database.session.statements[0])
    assert "user_id =" not in set_clause
    assert "paper_canonical_id =" not in set_clause
    assert "correlation_id =" in set_clause
