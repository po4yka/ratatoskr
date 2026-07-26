"""Tests for independent AI backup authorization lifecycle writes."""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert

from app.adapters.ai_backup.repository import AiBackupRepository
from app.db.models.ai_backup import (
    AiAccountBackup,
    AiBackupAuthorizationStatus,
    AiBackupService,
    AiBackupStatus,
)

if TYPE_CHECKING:
    from app.db.session import Database


class _FakeSession:
    def __init__(self, state: dict[str, object]) -> None:
        self._state = state

    async def scalar(self, _stmt: object) -> object | None:
        return self._state.get("row")

    def add(self, row: object) -> None:
        self._state["row"] = row

    async def execute(self, stmt: object) -> object:
        if not isinstance(stmt, Insert):
            raise AssertionError(f"Unexpected statement: {stmt!r}")
        statements = cast("list[Insert]", self._state.setdefault("statements", []))
        statements.append(stmt)
        params = stmt.compile(dialect=postgresql.dialect()).params
        await asyncio.sleep(0)
        if self._state.get("row") is None:
            self.add(
                AiAccountBackup(
                    user_id=params["user_id"],
                    service=params["service"],
                    status=params["status"],
                    authorization_status=params["authorization_status"],
                    consecutive_failures=params["consecutive_failures"],
                )
            )
            return SimpleNamespace(rowcount=1)
        return SimpleNamespace(rowcount=0)


class _FakeCtx:
    def __init__(self, session: _FakeSession) -> None:
        self._s = session

    async def __aenter__(self) -> _FakeSession:
        return self._s

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _FakeDb:
    def __init__(self, row: object | None) -> None:
        self.state: dict[str, object] = {"statements": []}
        if row is not None:
            self.state["row"] = row

    def transaction(self) -> _FakeCtx:
        return _FakeCtx(_FakeSession(self.state))

    def session(self) -> _FakeCtx:
        return _FakeCtx(_FakeSession(self.state))


def _repository(row: object | None) -> AiBackupRepository:
    return AiBackupRepository(cast("Database", _FakeDb(row)))


async def test_first_ingest_creates_pending_unverified_status_row() -> None:
    db = _FakeDb(None)
    repo = AiBackupRepository(cast("Database", db))

    await asyncio.gather(
        repo.mark_authorization_unverified(1, AiBackupService.CLAUDE),
        repo.mark_authorization_unverified(1, AiBackupService.CLAUDE),
    )

    row = await repo.get(1, AiBackupService.CLAUDE)
    assert row is not None
    assert row.user_id == 1
    assert row.service == AiBackupService.CLAUDE
    assert row.status == AiBackupStatus.PENDING
    assert row.authorization_status == AiBackupAuthorizationStatus.UNVERIFIED
    assert row.authorization_checked_at is None
    assert row.consecutive_failures == 0

    statements = cast("list[Insert]", db.state["statements"])
    assert len(statements) == 2
    for statement in statements:
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert "ON CONFLICT (user_id, service) DO NOTHING" in sql


async def test_mark_authorization_unverified_preserves_backup_outcome() -> None:
    last = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
    row = AiAccountBackup(
        user_id=1,
        service=AiBackupService.CLAUDE,
        status=AiBackupStatus.OK,
        authorization_status=AiBackupAuthorizationStatus.EXPIRED,
        last_backed_up_at=last,
        last_error="401",
        last_error_category="auth_expired",
        consecutive_failures=3,
    )
    repo = _repository(row)
    await repo.mark_authorization_unverified(1, AiBackupService.CLAUDE)
    assert row.status == AiBackupStatus.OK
    assert row.authorization_status == AiBackupAuthorizationStatus.UNVERIFIED
    assert row.authorization_checked_at is None
    assert row.last_backed_up_at == last  # NOT advanced — outage window stays in scope
    assert row.last_error is None
    assert row.consecutive_failures == 3


async def test_mark_authorization_missing_preserves_backup_outcome() -> None:
    row = AiAccountBackup(
        user_id=1,
        service=AiBackupService.CLAUDE,
        status=AiBackupStatus.OK,
        authorization_status=AiBackupAuthorizationStatus.VALID,
    )
    repo = _repository(row)
    await repo.mark_authorization_missing(1, AiBackupService.CLAUDE)
    assert row.status == AiBackupStatus.OK
    assert row.authorization_status == AiBackupAuthorizationStatus.MISSING
    assert row.authorization_checked_at is not None


async def test_mark_auth_expired_preserves_backup_outcome() -> None:
    last = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
    row = AiAccountBackup(
        user_id=1,
        service=AiBackupService.CLAUDE,
        status=AiBackupStatus.OK,
        authorization_status=AiBackupAuthorizationStatus.VALID,
        last_backed_up_at=last,
    )
    repo = _repository(row)
    await repo.mark_auth_expired(1, AiBackupService.CLAUDE, "re-ingest required")
    assert row.status == AiBackupStatus.OK
    assert row.authorization_status == AiBackupAuthorizationStatus.EXPIRED
    assert row.last_backed_up_at == last
    assert row.last_error == "re-ingest required"
