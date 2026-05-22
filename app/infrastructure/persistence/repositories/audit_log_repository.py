"""SQLAlchemy implementation of the audit log repository."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from app.db.json_utils import prepare_json_payload
from app.db.models import AuditLog

if TYPE_CHECKING:
    from app.db.session import Database


class AuditLogRepositoryAdapter:
    """Adapter for audit logging operations."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_insert_audit_log(
        self,
        log_level: str,
        event_type: str,
        details: dict[str, Any] | None = None,
    ) -> int:
        """Insert a new audit log record."""
        async with self._database.transaction() as session:
            log = AuditLog(
                level=log_level,
                event=event_type,
                details_json=prepare_json_payload(details),
            )
            add_result = session.add(log)
            if inspect.isawaitable(add_result):
                await add_result
            await session.flush()
            return log.id
