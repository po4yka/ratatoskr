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

if TYPE_CHECKING:
    from app.adapters.content.streaming.stream_hub import StreamHub
    from app.application.dto.stream_enums import ProcessingStage


class StreamHubStreamSink:
    """Structural implementation of ``StreamSinkPort`` over ``StreamHub``."""

    def __init__(self, hub: StreamHub | None = None) -> None:
        self._hub = hub

    def _publish(
        self, request_id: str, kind: str, payload: dict[str, Any], correlation_id: str
    ) -> None:
        hub = self._hub or get_stream_hub()
        hub.publish(request_id, StreamEvent.now(kind, payload, correlation_id))

    async def stage(
        self, *, request_id: str, correlation_id: str, stage: ProcessingStage | str
    ) -> None:
        # ProcessingStage is a StrEnum; StagePayload validation in StreamEvent.now
        # normalizes the enum and its str value to the same payload, so passing
        # either is byte-identical to the legacy producer.
        self._publish(request_id, "stage", {"stage": stage}, correlation_id)

    async def section(
        self,
        *,
        request_id: str,
        correlation_id: str,
        section: str,
        content: str,
        partial: bool = False,
    ) -> None:
        self._publish(
            request_id,
            "section",
            {"section": section, "content": content, "partial": partial},
            correlation_id,
        )

    async def warning(
        self, *, request_id: str, correlation_id: str, code: str, message: str
    ) -> None:
        self._publish(
            request_id,
            "warning",
            {"code": code, "message": message, "correlation_id": correlation_id},
            correlation_id,
        )

    async def done(self, *, request_id: str, correlation_id: str, summary_id: str | None) -> None:
        self._publish(
            request_id,
            "done",
            {"summary_id": summary_id, "request_id": request_id},
            correlation_id,
        )

    async def error(self, *, request_id: str, correlation_id: str, code: str, message: str) -> None:
        self._publish(
            request_id,
            "error",
            {"code": code, "message": message, "correlation_id": correlation_id},
            correlation_id,
        )
