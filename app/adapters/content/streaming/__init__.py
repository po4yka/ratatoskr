"""Streaming pub/sub primitives for the URL processing pipeline.

``StreamHub`` is the pub/sub surface (SSE + Telegram-draft consumers). The
*producer* under the graph rewrite is the ``stream_sink`` port (ADR-0017): the
summarize node emits via :class:`StreamHubStreamSink`, fed by
:class:`GraphEventBridge` which translates LangGraph ``astream_events`` into
``StreamHub`` events. The legacy inline ``publish('stage'/'section')`` producer
sites survive until the T9 hard cutover.

Public API re-exported from sub-modules:

- ``StreamHub`` / ``get_stream_hub`` — process-wide pub/sub hub
- ``StreamEvent`` / ``StreamEventKind`` — event envelope and kind discriminator
- ``StagePayload`` / ``SectionPayload`` / ``DonePayload`` / ``ErrorPayload`` — payload models
- ``SummarySectionSnapshot`` / ``SummarySectionStreamAssembler`` — incremental section assembler
- ``StreamHubStreamSink`` — the ``StreamSinkPort`` adapter over ``StreamHub``
- ``GraphEventBridge`` — ``astream_events`` → ``StreamSinkPort`` translator
"""

from app.adapters.content.streaming.events import (
    DonePayload,
    ErrorPayload,
    SectionPayload,
    StagePayload,
    StreamEvent,
    StreamEventKind,
    WarningPayload,
)
from app.adapters.content.streaming.graph_event_bridge import GraphEventBridge
from app.adapters.content.streaming.section_assembler import (
    SummarySectionSnapshot,
    SummarySectionStreamAssembler,
)
from app.adapters.content.streaming.stream_hub import (
    StreamHub,
    get_stream_hub,
)
from app.adapters.content.streaming.stream_sink_hub import StreamHubStreamSink

__all__ = [
    "DonePayload",
    "ErrorPayload",
    "GraphEventBridge",
    "SectionPayload",
    "StagePayload",
    "StreamEvent",
    "StreamEventKind",
    "StreamHub",
    "StreamHubStreamSink",
    "SummarySectionSnapshot",
    "SummarySectionStreamAssembler",
    "WarningPayload",
    "get_stream_hub",
]
