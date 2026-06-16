"""End-to-end (ADR-0017): the real ``_dispatch_summary_token`` producer ->
langgraph ``astream_events`` -> ``GraphEventBridge`` -> StreamSink section events.

Requires the optional ``graph`` extra; skipped where langgraph is absent (the
no-graph-extra CI invariant)."""

from __future__ import annotations

from typing import Any, TypedDict

import pytest

pytest.importorskip("langgraph")

from app.adapters.content.streaming.graph_event_bridge import GraphEventBridge
from app.application.graphs.summarize.nodes.summarize import (
    _dispatch_summary_token,
)


class RecordingSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def stage(self, *, request_id: str, correlation_id: str, stage: Any) -> None:
        self.calls.append(("stage", {"stage": str(stage)}))

    async def section(
        self,
        *,
        request_id: str,
        correlation_id: str,
        section: str,
        content: str,
        partial: bool = False,
    ) -> None:
        self.calls.append(("section", {"section": section, "content": content}))

    async def warning(self, **kw: Any) -> None:
        self.calls.append(("warning", kw))

    async def done(self, **kw: Any) -> None:
        self.calls.append(("done", kw))

    async def error(self, **kw: Any) -> None:
        self.calls.append(("error", kw))


class _S(TypedDict, total=False):
    summary: dict[str, Any]


async def test_dispatched_tokens_reach_the_sink_as_section_events() -> None:
    from langgraph.graph import END, START, StateGraph

    async def producer(state: _S) -> dict[str, Any]:
        # Real producer path: dispatch JSON deltas exactly as the summarize node does.
        for delta in ['{"summary_250": "Hello', ' world", "tldr": "Hi"}']:
            await _dispatch_summary_token(delta)
        return {"summary": {"summary_250": "Hello world", "tldr": "Hi"}}

    builder = StateGraph(_S)
    builder.add_node("producer", producer)
    builder.add_edge(START, "producer")
    builder.add_edge("producer", END)
    graph = builder.compile()

    sink = RecordingSink()
    bridge = GraphEventBridge(sink=sink, request_id="1", correlation_id="c")
    async for event in graph.astream_events({}, version="v2"):
        await bridge.dispatch(event)

    sections = [(c[1]["section"], c[1]["content"]) for c in sink.calls if c[0] == "section"]
    # Live previews arrived (the whole point of ADR-0017), ending in final values.
    assert ("summary_250", "Hello world") in sections
    assert ("tldr", "Hi") in sections
    # Terminal done/error never came from the bridge (single emitter is the publisher).
    assert not any(c[0] in ("done", "error") for c in sink.calls)


async def test_dispatcher_is_safe_without_astream_context() -> None:
    # Outside an astream_events run there is no callback manager; the best-effort
    # dispatcher must swallow that and never raise (ADR-0011/0017).
    await _dispatch_summary_token('{"summary_250": "x"}')
    await _dispatch_summary_token("")
