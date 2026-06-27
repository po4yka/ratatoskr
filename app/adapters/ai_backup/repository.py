"""Persistence adapter for AI account backup state.

``AiBackupRepository`` wraps the ``ai_account_backups`` table and provides the
query/write operations the Taskiq job and the REST/Telegram surfaces rely on.
Writes go through ``db.transaction()``, reads through ``db.session()``.

Every write carries a ``user_id`` predicate as a defense-in-depth IDOR guard
(project Operating Rule 12): the update silently no-ops if the row does not
belong to the expected user, even though the deployment is single-tenant.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import and_, select

from app.db.models.ai_backup import AiAccountBackup, AiBackupService, AiBackupStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.adapters.ai_backup.errors import AiBackupErrorCategory
    from app.db.session import Database

# Backoff after repeated transient failures: hours = min(consecutive, cap).
_BACKOFF_CAP_HOURS = 24


class AiBackupRepository:
    """SQLAlchemy adapter for ``ai_account_backups`` table access."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get(self, user_id: int, service: AiBackupService) -> AiAccountBackup | None:
        async with self._db.session() as session:
            return await session.scalar(
                select(AiAccountBackup).where(
                    and_(
                        AiAccountBackup.user_id == user_id,
                        AiAccountBackup.service == service,
                    )
                )
            )

    async def list_for_user(self, user_id: int) -> list[AiAccountBackup]:
        async with self._db.session() as session:
            rows = (
                await session.scalars(
                    select(AiAccountBackup)
                    .where(AiAccountBackup.user_id == user_id)
                    .order_by(AiAccountBackup.id)
                )
            ).all()
        return list(rows)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def ensure(self, user_id: int, service: AiBackupService) -> AiAccountBackup:
        """Return the existing row for (user, service) or create a PENDING one.

        Existing rows are returned untouched so in-progress lifecycle state is
        preserved across runs.
        """
        async with self._db.transaction() as session:
            existing = await session.scalar(
                select(AiAccountBackup).where(
                    and_(
                        AiAccountBackup.user_id == user_id,
                        AiAccountBackup.service == service,
                    )
                )
            )
            if existing is not None:
                return existing

            row = AiAccountBackup(
                user_id=user_id,
                service=service,
                status=AiBackupStatus.PENDING,
                consecutive_failures=0,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
        return row

    async def record_success(
        self,
        user_id: int,
        service: AiBackupService,
        *,
        counts: dict | None = None,
        backup_path: str | None = None,
    ) -> None:
        """Persist a successful backup outcome: reset failure counters, record path."""
        now = dt.datetime.now(tz=dt.UTC)
        async with self._db.transaction() as session:
            row = await self._load_for_update(session, user_id, service)
            if row is None:
                return
            row.status = AiBackupStatus.OK
            row.last_backed_up_at = now
            row.last_attempt_at = now
            row.consecutive_failures = 0
            row.backoff_until = None
            row.last_error = None
            row.last_error_category = None
            if counts is not None:
                row.counts_json = counts
            if backup_path is not None:
                row.last_backup_path = backup_path

    async def record_failure(
        self,
        user_id: int,
        service: AiBackupService,
        *,
        category: AiBackupErrorCategory,
        message: str,
    ) -> None:
        """Persist a transient failure: increment counters, set a backoff window."""
        now = dt.datetime.now(tz=dt.UTC)
        async with self._db.transaction() as session:
            row = await self._load_for_update(session, user_id, service)
            if row is None:
                return
            row.status = AiBackupStatus.FAILED
            row.last_attempt_at = now
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
            row.total_failures = (row.total_failures or 0) + 1
            row.last_failure_at = now
            row.last_error = message[:4000] if message else None
            row.last_error_category = category.value
            hours = min(row.consecutive_failures, _BACKOFF_CAP_HOURS)
            row.backoff_until = now + dt.timedelta(hours=hours)

    async def mark_auth_expired(self, user_id: int, service: AiBackupService, message: str) -> None:
        """Halt a service whose session has expired; the operator must re-supply one.

        No backoff is set — the service stays halted until a fresh session is
        ingested (which resets the row via ``record_success``).
        """
        now = dt.datetime.now(tz=dt.UTC)
        async with self._db.transaction() as session:
            row = await self._load_for_update(session, user_id, service)
            if row is None:
                return
            row.status = AiBackupStatus.AUTH_EXPIRED
            row.last_attempt_at = now
            row.last_failure_at = now
            row.last_error = message[:4000] if message else None
            row.last_error_category = "auth_expired"

    async def clear_auth_expired(self, user_id: int, service: AiBackupService) -> None:
        """Lift an AUTH_EXPIRED halt after a fresh session is supplied.

        Resets status + error fields to PENDING but deliberately does NOT touch
        ``last_backed_up_at`` -- using ``record_success`` here would advance it to
        now and make the next incremental run skip everything that changed during
        the outage window. No-op unless the row is currently AUTH_EXPIRED.
        """
        async with self._db.transaction() as session:
            row = await self._load_for_update(session, user_id, service)
            if row is None or row.status != AiBackupStatus.AUTH_EXPIRED:
                return
            row.status = AiBackupStatus.PENDING
            row.consecutive_failures = 0
            row.backoff_until = None
            row.last_error = None
            row.last_error_category = None

    async def _load_for_update(
        self, session: AsyncSession, user_id: int, service: AiBackupService
    ) -> AiAccountBackup | None:
        return await session.scalar(
            select(AiAccountBackup).where(
                and_(
                    AiAccountBackup.user_id == user_id,
                    AiAccountBackup.service == service,
                )
            )
        )


__all__ = ["AiBackupRepository"]
