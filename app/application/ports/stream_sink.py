"""Streaming sink port (ADR-0017).

Framework-agnostic seam between the summarize graph and the in-process
``StreamHub`` pub/sub. The bridge that consumes LangGraph ``astream_events``
(:mod:`app.adapters.content.streaming.graph_event_bridge`) and the
``StreamHubStreamSink`` adapter (:mod:`app.adapters.content.streaming.stream_sink_hub`)
land with T8; this module owns the minimal publish surface.

The port deliberately imports no ``StreamHub`` / ``StreamEvent`` / ``langgraph``
types -- streamed output is an ephemeral side-channel, never checkpoint state
(ADR-0011). The concrete ``StreamEvent`` envelope and payload models live in the
adapter layer; this port speaks only in primitives plus the neutral
``ProcessingStage`` enum from ``app.application.dto`` (same layer, no runtime deps).

Every method carries ``request_id`` + ``correlation_id`` per call: the sink is a
process-wide singleton (DI-injected into ``SummarizeDeps``), so per-invocation
identity travels with the call, not the instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.application.dto.stream_enums import ProcessingStage


@runtime_checkable
class StreamSinkPort(Protocol):
    """Publish streaming progress events for an in-flight request.

    Mirrors the five public ``ProgressEventKind`` shapes (stage / section /
    warning / done / error) so the in-process ``StreamHub`` is a structural
    implementation of this port and SSE + Telegram-draft consumers need no change.
    """

    async def stage(
        self, *, request_id: str, correlation_id: str, stage: ProcessingStage | str
    ) -> None:
        """Emit a pipeline ``stage`` transition (extracting/summarizing/...)."""
        ...

    async def section(
        self,
        *,
        request_id: str,
        correlation_id: str,
        section: str,
        content: str,
        partial: bool = False,
    ) -> None:
        """Emit a partial summary ``section`` snapshot (summary_250/tldr/...)."""
        ...

    async def warning(
        self, *, request_id: str, correlation_id: str, code: str, message: str
    ) -> None:
        """Emit a non-terminal ``warning``."""
        ...

    async def done(self, *, request_id: str, correlation_id: str, summary_id: str | None) -> None:
        """Emit the terminal ``done`` event (stream-terminating)."""
        ...

    async def error(self, *, request_id: str, correlation_id: str, code: str, message: str) -> None:
        """Emit the terminal ``error`` event (stream-terminating)."""
        ...
