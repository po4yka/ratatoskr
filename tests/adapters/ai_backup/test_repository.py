"""Tests for AiBackupRepository.clear_auth_expired (fake DB session)."""

from __future__ import annotations

import datetime as dt

from app.adapters.ai_backup.repository import AiBackupRepository
from app.db.models.ai_backup import AiAccountBackup, AiBackupService, AiBackupStatus


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


async def test_clear_auth_expired_preserves_last_backed_up_at() -> None:
    last = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
    row = AiAccountBackup(
        user_id=1,
        service=AiBackupService.CLAUDE,
        status=AiBackupStatus.AUTH_EXPIRED,
        last_backed_up_at=last,
        last_error="401",
        last_error_category="auth_expired",
        consecutive_failures=3,
    )
    repo = AiBackupRepository(_FakeDb(row))
    await repo.clear_auth_expired(1, AiBackupService.CLAUDE)
    assert row.status == AiBackupStatus.PENDING
    assert row.last_backed_up_at == last  # NOT advanced — outage window stays in scope
    assert row.last_error is None
    assert row.consecutive_failures == 0


async def test_clear_auth_expired_noop_when_not_expired() -> None:
    row = AiAccountBackup(user_id=1, service=AiBackupService.CLAUDE, status=AiBackupStatus.OK)
    repo = AiBackupRepository(_FakeDb(row))
    await repo.clear_auth_expired(1, AiBackupService.CLAUDE)
    assert row.status == AiBackupStatus.OK  # unchanged
