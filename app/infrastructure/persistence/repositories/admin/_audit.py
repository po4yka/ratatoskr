"""Admin read queries for the audit log."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, func, select

from app.db.models import AuditLog
from app.infrastructure.persistence.repositories.admin._helpers import _parse_since, isotime

if TYPE_CHECKING:
    from app.db.session import Database


class AuditLogReadRepository:
    """Read-side queries for audit log browsing."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_audit_log(
        self,
        *,
        action: str | None,
        user_id_filter: int | None,
        since: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        since_dt = _parse_since(since)
        async with self._database.session() as session:
            conditions = []
            if action:
                conditions.append(AuditLog.event == action)
            if since_dt is not None:
                conditions.append(AuditLog.ts >= since_dt)

            query = select(AuditLog)
            count_query = select(func.count(AuditLog.id))
            if conditions:
                query = query.where(*conditions)
                count_query = count_query.where(*conditions)

            total = int(await session.scalar(count_query) or 0)
            logs: list[dict[str, Any]] = []
            entries = (
                await session.execute(
                    query.order_by(desc(AuditLog.ts)).offset(offset).limit(limit)
                )
            ).scalars()
            for entry in entries:
                details = entry.details_json
                if user_id_filter is not None:
                    if not isinstance(details, dict) or details.get("user_id") != user_id_filter:
                        total -= 1
                        continue
                logs.append(
                    {
                        "id": entry.id,
                        "timestamp": isotime(entry.ts),
                        "level": entry.level,
                        "event": entry.event,
                        "details": details,
                    }
                )

            return {"logs": logs, "total": total, "limit": limit, "offset": offset}
