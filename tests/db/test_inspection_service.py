"""Unit tests for DatabaseInspectionService transaction handling (no live DB).

These reproduce PostgreSQL's aborted-transaction semantics with a fake session so
the rollback-after-failed-count behavior is exercised without a real database.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import AuditLog, Request, Summary
from app.db.runtime import inspection
from app.db.runtime.inspection import DatabaseInspectionService


class _AbortedTransactionError(SQLAlchemyError):
    """Mimics PostgreSQL 'current transaction is aborted' after a failed statement."""


class _FakeSession:
    """Reproduces PostgreSQL aborted-transaction semantics.

    Once a statement raises, every later statement fails with an aborted-transaction
    error until ``rollback()`` clears the state -- exactly the condition that let one
    failed table count cascade into failing every subsequent query.
    """

    def __init__(self, *, fail_table: str) -> None:
        self._fail_table = fail_table
        self._aborted = False
        self.rollback_calls = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def rollback(self) -> None:
        self.rollback_calls += 1
        self._aborted = False

    def _guard(self) -> None:
        if self._aborted:
            raise _AbortedTransactionError("current transaction is aborted")

    async def scalar(self, stmt):
        self._guard()
        # A LIMIT clause marks the _last_created_at probe, not a table count.
        if stmt._limit_clause is not None:
            return None
        table_name = stmt.get_final_froms()[0].name
        if table_name == self._fail_table:
            self._aborted = True
            raise _AbortedTransactionError(f"boom counting {table_name}")
        return 7

    async def execute(self, _stmt):
        # _requests_by_status: an empty result is enough for this test.
        self._guard()
        return []


def _service_with(session: _FakeSession, monkeypatch, models) -> DatabaseInspectionService:
    monkeypatch.setattr(inspection, "ALL_MODELS", models)
    return DatabaseInspectionService(
        session_maker=lambda: session,  # type: ignore[arg-type]
        logger=logging.getLogger("test_inspection_service"),
    )


@pytest.mark.asyncio
async def test_overview_recovers_after_one_table_count_fails(monkeypatch) -> None:
    # Summary's count fails and aborts the transaction. The rollback lets the
    # tables before AND after it still be counted, and the post-loop status /
    # timestamp queries succeed instead of cascading into a total failure.
    session = _FakeSession(fail_table=Summary.__tablename__)
    service = _service_with(session, monkeypatch, [Request, Summary, AuditLog])

    overview = await service.async_get_database_overview()

    assert set(overview["tables"]) == {Request.__tablename__, AuditLog.__tablename__}
    assert overview["errors"] == [f"Failed to count rows for table '{Summary.__tablename__}'"]
    assert session.rollback_calls == 1
    # These run after the loop and would have raised on a poisoned transaction.
    assert overview["requests_by_status"] == {}
    assert "last_request_at" in overview


@pytest.mark.asyncio
async def test_overview_counts_every_table_when_none_fail(monkeypatch) -> None:
    session = _FakeSession(fail_table="__no_such_table__")
    service = _service_with(session, monkeypatch, [Request, Summary, AuditLog])

    overview = await service.async_get_database_overview()

    assert set(overview["tables"]) == {
        Request.__tablename__,
        Summary.__tablename__,
        AuditLog.__tablename__,
    }
    assert "errors" not in overview
    assert session.rollback_calls == 0
