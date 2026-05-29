"""Stream event types and payload models for the in-process pub/sub hub.

These primitives carry progress notifications from the URL processing pipeline
to SSE consumers and Telegram draft-edit subscribers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from app.application.dto.stream_enums import ProcessingStage, ProgressEventKind

StreamEventKind = ProgressEventKind


class StagePayload(BaseModel):
    stage: ProcessingStage


class SectionPayload(BaseModel):
    section: str
    content: str
    partial: bool = False


class DonePayload(BaseModel):
    # ``summary_id`` is None when the pipeline ended without producing one.
    summary_id: str | None
    request_id: str


class ErrorPayload(BaseModel):
    code: str
    message: str
    correlation_id: str


class WarningPayload(BaseModel):
    code: str
    message: str
    correlation_id: str | None = None


_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "stage": StagePayload,
    "section": SectionPayload,
    "warning": WarningPayload,
    "done": DonePayload,
    "error": ErrorPayload,
}


@dataclass(slots=True, frozen=True)
class StreamEvent:
    kind: StreamEventKind
    payload: dict[str, Any]
    timestamp: datetime
    correlation_id: str

    @classmethod
    def now(
        cls,
        kind: StreamEventKind | str,
        payload: BaseModel | dict[str, Any],
        correlation_id: str,
    ) -> StreamEvent:
        kind_value = kind.value if isinstance(kind, ProgressEventKind) else kind
        model_cls = _PAYLOAD_MODELS[kind_value]
        raw = payload.model_dump() if isinstance(payload, BaseModel) else payload
        validated = model_cls.model_validate(raw)
        return cls(
            kind=ProgressEventKind(kind_value),
            payload=validated.model_dump(),
            timestamp=datetime.now(UTC),
            correlation_id=correlation_id,
        )


__all__ = [
    "DonePayload",
    "ErrorPayload",
    "ProgressEventKind",
    "SectionPayload",
    "StagePayload",
    "StreamEvent",
    "StreamEventKind",
    "WarningPayload",
]
