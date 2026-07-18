"""Persistence adapter for content-free summarize graph chronology."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.db.models import ProgressEvent
from app.db.types import _utcnow

if TYPE_CHECKING:
    from app.db.session import Database


class ProgressEventGraphRunLedger:
    """Store graph node lifecycle transitions as dedicated progress-event rows."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def record_node(
        self, *, request_id: int, correlation_id: str, node: str, status: str
    ) -> None:
        async with self._database.transaction() as session:
            sequence = (
                int(
                    await session.scalar(
                        select(func.coalesce(func.max(ProgressEvent.sequence), 0)).where(
                            ProgressEvent.request_id == request_id
                        )
                    )
                    or 0
                )
                + 1
            )
            event_id = hashlib.sha256(f"graph:{request_id}:{sequence}".encode()).hexdigest()
            session.add(
                ProgressEvent(
                    event_id=f"graph-{event_id[:24]}",
                    request_id=request_id,
                    sequence=sequence,
                    kind="graph_node",
                    stage=node,
                    status=status,
                    message=None,
                    progress=None,
                    payload=None,
                    correlation_id=correlation_id,
                    created_at=_utcnow(),
                )
            )
