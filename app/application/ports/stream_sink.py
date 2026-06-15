"""Streaming sink port (ADR-0017).

Framework-agnostic seam between the summarize graph and the in-process
``StreamHub`` pub/sub. The bridge that consumes LangGraph ``astream_events``
and the ``StreamHubStreamSink`` adapter land with T8; T3 scaffolds the minimal
publish surface. The port deliberately imports no ``StreamHub`` /
``StreamEvent`` / ``langgraph`` types -- streamed output is an ephemeral
side-channel, never checkpoint state (ADR-0011).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StreamSinkPort(Protocol):
    """Publish a streaming event for an in-flight request."""

    def publish(self, request_id: str, event: Any) -> None:
        """Fan out ``event`` to subscribers of ``request_id``.

        ``event`` is a framework-agnostic stream event; the concrete
        ``StreamEvent`` lives in the adapter layer and is intentionally not
        referenced here. Mirrors the existing ``StreamHub.publish`` surface so
        the hub is a structural implementation of this port.
        """
        ...
