from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from app.db.json_utils import prepare_json_payload
from app.db.models import ProgressEvent
from app.db.types import _utcnow, model_to_dict


@dataclass(frozen=True, slots=True)
class ProgressEventRecord:
    event_id: str
    request_id: int
    sequence: int
    kind: str
    stage: str | None
    status: str | None
    message: str | None
    progress: float | None
    payload: dict[str, Any] | None
    created_at: Any
    correlation_id: str | None

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> ProgressEventRecord:
        payload = row.get("payload")
        return cls(
            event_id=str(row["event_id"]),
            request_id=int(row["request_id"]),
            sequence=int(row["sequence"]),
            kind=str(row["kind"]),
            stage=row.get("stage"),
            status=row.get("status"),
            message=row.get("message"),
            progress=float(row["progress"]) if row.get("progress") is not None else None,
            payload=payload if isinstance(payload, dict) else None,
            created_at=row.get("created_at"),
            correlation_id=row.get("correlation_id"),
        )

    def as_sse_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "request_id": self.request_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "stage": self.stage,
            "status": self.status,
            "message": self.message,
            "progress": self.progress,
            "payload": self.payload or {},
            "created_at": (
                self.created_at.isoformat().replace("+00:00", "Z")
                if hasattr(self.created_at, "isoformat")
                else str(self.created_at)
            ),
            "correlation_id": self.correlation_id,
        }


class ProgressEventRepository:
    """Durable progress-event store with monotonic per-request sequences."""

    def __init__(self, database: Any) -> None:
        self._database = database

    async def append(
        self,
        *,
        request_id: int,
        kind: str,
        stage: str | None,
        status: str | None,
        message: str | None,
        progress: float | None,
        payload: dict[str, Any] | None,
        correlation_id: str | None,
    ) -> ProgressEventRecord:
        now = _utcnow()
        async with self._database.transaction() as session:
            latest_sequence = await session.scalar(
                select(func.max(ProgressEvent.sequence)).where(
                    ProgressEvent.request_id == request_id
                )
            )
            sequence = int(latest_sequence or 0) + 1
            event_id = _event_id(request_id=request_id, sequence=sequence)
            row = ProgressEvent(
                event_id=event_id,
                request_id=request_id,
                sequence=sequence,
                kind=kind,
                stage=stage,
                status=status,
                message=message,
                progress=progress,
                payload=prepare_json_payload(payload or {}, default={}),
                correlation_id=correlation_id,
                created_at=now,
            )
            session.add(row)
            await session.flush()
            return ProgressEventRecord.from_mapping(model_to_dict(row) or {})

    async def list_after_sequence(
        self,
        *,
        request_id: int,
        sequence: int,
        limit: int = 100,
    ) -> list[ProgressEventRecord]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(ProgressEvent)
                    .where(
                        ProgressEvent.request_id == request_id,
                        ProgressEvent.sequence > sequence,
                    )
                    .order_by(ProgressEvent.sequence)
                    .limit(limit)
                )
            ).scalars()
            return [ProgressEventRecord.from_mapping(model_to_dict(row) or {}) for row in rows]

    async def get_latest(self, request_id: int) -> ProgressEventRecord | None:
        async with self._database.session() as session:
            row = await session.scalar(
                select(ProgressEvent)
                .where(ProgressEvent.request_id == request_id)
                .order_by(ProgressEvent.sequence.desc())
                .limit(1)
            )
            return ProgressEventRecord.from_mapping(model_to_dict(row) or {}) if row else None

    async def sequence_for_event_id(self, *, request_id: int, event_id: str) -> int | None:
        async with self._database.session() as session:
            value = await session.scalar(
                select(ProgressEvent.sequence).where(
                    ProgressEvent.request_id == request_id,
                    ProgressEvent.event_id == event_id,
                )
            )
            return int(value) if value is not None else None


def _event_id(*, request_id: int, sequence: int) -> str:
    suffix = hashlib.sha256(f"{request_id}:{sequence}".encode()).hexdigest()[:12]
    return f"req-{request_id}-{sequence}-{suffix}"
