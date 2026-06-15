"""Database inspection and integrity services."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import ALL_MODELS, AuditLog, Request, Summary

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class DatabaseInspectionService:
    """Expose PostgreSQL integrity and diagnostics operations."""

    def __init__(self, *, session_maker: async_sessionmaker[AsyncSession], logger: Any) -> None:
        self._session_maker = session_maker
        self._logger = logger
        self._last_rollback_count: int | None = None
        self._last_rollback_checked_at = 0.0

    async def async_check_integrity(self) -> tuple[bool, str]:
        try:
            async with self._session_maker() as session:
                await session.execute(text("SELECT 1"))
                rollback_count = await session.scalar(
                    text(
                        "SELECT xact_rollback "
                        "FROM pg_stat_database "
                        "WHERE datname = current_database()"
                    )
                )
        except Exception as exc:
            self._logger.error("db_integrity_check_failed", extra={"error": str(exc)})
            return False, str(exc)

        current = int(rollback_count or 0)
        now = time.time()
        previous = self._last_rollback_count
        previous_checked_at = self._last_rollback_checked_at
        self._last_rollback_count = current
        self._last_rollback_checked_at = now

        if previous is not None and now - previous_checked_at <= 3600:
            delta = current - previous
            if delta > 0:
                reason = f"xact_rollback increased by {delta} in the last hour"
                self._logger.warning("db_rollback_delta_detected", extra={"delta": delta})
                return False, reason

        return True, "ok"

    def get_database_overview(self) -> dict[str, Any]:
        return cast("dict[str, Any]", _run_sync(self.async_get_database_overview()))

    async def async_get_database_overview(self) -> dict[str, Any]:
        overview: dict[str, Any] = {"tables": {}, "errors": []}
        async with self._session_maker() as session:
            for model in ALL_MODELS:
                table_name = model.__tablename__
                try:
                    count_stmt = select(func.count()).select_from(model)
                    overview["tables"][table_name] = int(await session.scalar(count_stmt) or 0)
                except SQLAlchemyError as exc:
                    overview["errors"].append(f"Failed to count rows for table '{table_name}'")
                    self._logger.exception(
                        "db_table_count_failed",
                        extra={"table": table_name, "error": str(exc)},
                    )

            overview["requests_by_status"] = await self._requests_by_status(session)
            overview["last_request_at"] = await self._last_created_at(session, Request.created_at)
            overview["last_summary_at"] = await self._last_created_at(session, Summary.created_at)
            overview["last_audit_at"] = await self._last_created_at(session, AuditLog.ts)

        tables = overview["tables"]
        overview["total_requests"] = int(tables.get("requests", 0))
        overview["total_summaries"] = int(tables.get("summaries", 0))
        if not overview["errors"]:
            overview.pop("errors")
        return overview

    async def async_database_size_mb(self) -> float:
        """Return the current PostgreSQL database size in MiB."""
        async with self._session_maker() as session:
            size_bytes = await session.scalar(text("SELECT pg_database_size(current_database())"))
        return round(float(size_bytes or 0) / (1024 * 1024), 1)

    async def _requests_by_status(self, session: AsyncSession) -> dict[str, int]:
        rows = await session.execute(
            select(Request.status, func.count(Request.id)).group_by(Request.status)
        )
        return {str(status or "unknown"): int(count) for status, count in rows}

    @staticmethod
    async def _last_created_at(session: AsyncSession, column: Any) -> Any:
        return await session.scalar(select(column).order_by(column.desc()).limit(1))

    async def async_verify_processing_integrity(
        self,
        *,
        required_fields: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        del required_fields, limit
        return {
            "overview": await self.async_get_database_overview(),
            "posts": {
                "checked": 0,
                "with_summary": 0,
                "missing_summary": [],
                "missing_fields": [],
                "errors": ["processing integrity SQLAlchemy port is tracked in R3"],
                "links": {"total_links": 0, "posts_with_links": 0, "missing_data": []},
                "reprocess": [],
            },
        }


def _run_sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    msg = "Synchronous database inspection methods cannot run inside an active event loop"
    raise RuntimeError(msg)
