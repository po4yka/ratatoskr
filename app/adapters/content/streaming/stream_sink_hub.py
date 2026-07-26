"""``StreamHubStreamSink`` -- the only ``StreamSinkPort`` adapter (ADR-0017).

Wraps the in-process :class:`StreamHub` and the ``StreamEvent`` envelope so the
summarize graph can emit progress without importing any streaming type. Every
method builds the exact ``Stage/Section/Warning/Done/ErrorPayload`` shape from
:mod:`app.adapters.content.streaming.events` and routes it through
``StreamEvent.now`` -- which validates the payload against the pydantic model --
so events are byte-for-byte identical to today's legacy producer and SSE +
Telegram-draft consumers need zero changes.

``StreamHub.publish`` is synchronous; the port surface is async so future sinks
can do real I/O. The hub is resolved lazily via ``get_stream_hub()`` unless one
is injected (tests pass a recording hub).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.content.streaming.events import StreamEvent
from app.adapters.content.streaming.stream_hub import get_stream_hub
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.adapters.content.streaming.stream_hub import StreamHub
    from app.application.dto.stream_enums import ProcessingStage


class StreamHubStreamSink:
    """Structural implementation of ``StreamSinkPort`` over ``StreamHub``."""

    def __init__(
        self, hub: StreamHub | None = None, progress_event_repo: Any | None = None
    ) -> None:
        self._hub = hub
        self._progress_event_repo = progress_event_repo

    async def _publish(
        self, request_id: str, kind: str, payload: dict[str, Any], correlation_id: str
    ) -> None:
        if self._progress_event_repo is not None:
            try:
                await self._progress_event_repo.append(
                    request_id=int(request_id),
                    kind=kind,
                    stage=str(payload.get("stage")) if payload.get("stage") is not None else None,
                    status="completed"
                    if kind == "done"
                    else "failed"
                    if kind == "error"
                    else "processing",
                    message=str(payload.get("content") or payload.get("message") or kind),
                    progress=None,
                    payload=payload,
                    correlation_id=correlation_id,
                )
            except Exception as exc:
                get_logger(__name__).warning(
                    "graph_progress_event_persist_failed",
                    extra={"request_id": request_id, "kind": kind, "error": str(exc)},
                )
        hub = self._hub or get_stream_hub()
        hub.publish(request_id, StreamEvent.now(kind, payload, correlation_id))

    async def stage(
        self, *, request_id: str, correlation_id: str, stage: ProcessingStage | str
    ) -> None:
        # ProcessingStage is a StrEnum; StagePayload validation in StreamEvent.now
        # normalizes the enum and its str value to the same payload, so passing
        # either is byte-identical to the legacy producer.
        await self._publish(request_id, "stage", {"stage": stage}, correlation_id)

    async def section(
        self,
        *,
        request_id: str,
        correlation_id: str,
        section: str,
        content: str,
        partial: bool = False,
    ) -> None:
        await self._publish(
            request_id,
            "section",
            {"section": section, "content": content, "partial": partial},
            correlation_id,
        )

    async def warning(
        self, *, request_id: str, correlation_id: str, code: str, message: str
    ) -> None:
        await self._publish(
            request_id,
            "warning",
            {"code": code, "message": message, "correlation_id": correlation_id},
            correlation_id,
        )

    async def done(self, *, request_id: str, correlation_id: str, summary_id: str | None) -> None:
        await self._publish(
            request_id,
            "done",
            {"summary_id": summary_id, "request_id": request_id},
            correlation_id,
        )

    async def error(self, *, request_id: str, correlation_id: str, code: str, message: str) -> None:
        await self._publish(
            request_id,
            "error",
            {"code": code, "message": message, "correlation_id": correlation_id},
            correlation_id,
        )
