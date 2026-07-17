"""Tests for independent AI backup authorization lifecycle writes."""

from __future__ import annotations

import datetime as dt

from app.adapters.ai_backup.repository import AiBackupRepository
from app.db.models.ai_backup import (
    AiAccountBackup,
    AiBackupAuthorizationStatus,
    AiBackupService,
    AiBackupStatus,
)


class _FakeSession:
    def __init__(self, row: object) -> None:
        self._row = row

    async def scalar(self, _stmt: object) -> object:
        return self._row


class _FakeCtx:
    def __init__(self, session: _FakeSession) -> None:
        self._s = session

    async def __aenter__(self) -> _FakeSession:
        return self._s

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _FakeDb:
    def __init__(self, row: object) -> None:
        self._row = row

    def transaction(self) -> _FakeCtx:
        return _FakeCtx(_FakeSession(self._row))


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
    repo = AiBackupRepository(_FakeDb(row))
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
    repo = AiBackupRepository(_FakeDb(row))
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
    repo = AiBackupRepository(_FakeDb(row))
    await repo.mark_auth_expired(1, AiBackupService.CLAUDE, "re-ingest required")
    assert row.status == AiBackupStatus.OK
    assert row.authorization_status == AiBackupAuthorizationStatus.EXPIRED
    assert row.last_backed_up_at == last
    assert row.last_error == "re-ingest required"
