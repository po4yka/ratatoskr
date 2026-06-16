"""The di/graphs streaming driver (run_summarize_graph_streamed) drives
astream_events through the bridge, captures final state, routes failures to the
single terminal path, and never lets stream buffers reach checkpoint state
(ADR-0011/0017)."""

from __future__ import annotations

from typing import Any

import app.di.graphs as graphs_mod
from app.application.graphs.summarize.graph import build_initial_state
from app.application.graphs.summarize.lifecycle import CallBudgetExceeded
from app.application.graphs.summarize.state import SummarizeState
from app.di.graphs import run_summarize_graph_streamed

_CID = "corr-xyz"
_RID = 7


class RecordingSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def stage(self, *, request_id: str, correlation_id: str, stage: Any) -> None:
        self.calls.append(("stage", {"stage": stage}))

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


class FakeGraph:
    """Minimal stand-in: astream_events yields canned events (no langgraph)."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def astream_events(self, state: Any, *, config: Any, version: str):
        assert version == "v2"
        assert config["configurable"]["thread_id"] == _CID  # thread_id is sacred
        for event in self._events:
            yield event


class RaisingGraph:
    async def astream_events(self, state: Any, *, config: Any, version: str):
        raise RuntimeError("node boom")
        yield  # pragma: no cover -- makes this an async generator


class BudgetGraph:
    async def astream_events(self, state: Any, *, config: Any, version: str):
        raise CallBudgetExceeded("budget")
        yield  # pragma: no cover


async def test_driver_streams_stages_sections_and_captures_final_state() -> None:
    sink = RecordingSink()
    graph = FakeGraph(
        [
            {
                "event": "on_chain_start",
                "name": "extract",
                "metadata": {"langgraph_node": "extract"},
            },
            {
                "event": "on_chain_start",
                "name": "summarize",
                "metadata": {"langgraph_node": "summarize"},
            },
            {"event": "on_custom_event", "name": "summary_token", "data": '{"summary_250": "Hi"}'},
            {
                "event": "on_chain_end",
                "name": "LangGraph",
                "data": {"output": {"request_id": _RID, "summary": {"x": 1}}},
            },
        ]
    )
    result = await run_summarize_graph_streamed(
        graph=graph, deps=object(), sink=sink, correlation_id=_CID, request_id=_RID, lang="en"
    )
    assert result == {"request_id": _RID, "summary": {"x": 1}}
    assert [c[0] for c in sink.calls] == ["stage", "stage", "section"]
    assert sink.calls[-1] == ("section", {"section": "summary_250", "content": "Hi"})


async def test_driver_routes_node_failure_to_terminal_path(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_route(state: Any, deps: Any, exc: BaseException, *, reason_code: str) -> str:
        captured["reason_code"] = reason_code
        captured["exc_type"] = type(exc).__name__
        return "Error ID: corr-xyz"

    monkeypatch.setattr(graphs_mod, "route_terminal_failure", fake_route)
    result = await run_summarize_graph_streamed(
        graph=RaisingGraph(),
        deps=object(),
        sink=RecordingSink(),
        correlation_id=_CID,
        request_id=_RID,
        lang="en",
    )
    assert result == {"error": "Error ID: corr-xyz", "correlation_id": _CID, "request_id": _RID}
    assert captured["reason_code"] == "GRAPH_NODE_FAILURE"
    assert captured["exc_type"] == "RuntimeError"


async def test_driver_maps_call_budget_exceeded_reason(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_route(state: Any, deps: Any, exc: BaseException, *, reason_code: str) -> str:
        captured["reason_code"] = reason_code
        return "Error ID: x"

    monkeypatch.setattr(graphs_mod, "route_terminal_failure", fake_route)
    await run_summarize_graph_streamed(
        graph=BudgetGraph(),
        deps=object(),
        sink=RecordingSink(),
        correlation_id=_CID,
        request_id=_RID,
        lang="en",
    )
    assert captured["reason_code"] == "GRAPH_CALL_BUDGET_EXCEEDED"


async def test_driver_ignores_nested_and_non_dict_chain_end() -> None:
    """Only the root on_chain_end (no parent_ids) with a dict output is captured;
    nested ends and non-dict outputs leave final_state empty."""
    sink = RecordingSink()
    graph = FakeGraph(
        [
            # nested node end carries parent_ids -> not the final state
            {
                "event": "on_chain_end",
                "name": "summarize",
                "parent_ids": ["root"],
                "data": {"output": {"request_id": 999}},
            },
            # root end but output is not a dict -> ignored
            {"event": "on_chain_end", "name": "LangGraph", "data": {"output": "not-a-dict"}},
        ]
    )
    result = await run_summarize_graph_streamed(
        graph=graph, deps=object(), sink=sink, correlation_id=_CID, request_id=_RID, lang="en"
    )
    # No valid root dict output captured -> the empty-dict fallback (best-effort,
    # not load-bearing; T9 parity asserts output via ainvoke).
    assert result == {}
    assert sink.calls == []


# --- resume does not replay a half-stream: no stream buffer in checkpoint state ---

_STREAM_BUFFER_MARKERS = ("stream", "token", "buffer", "delta")


def test_summarize_state_carries_no_stream_buffer_field() -> None:
    keys = set(SummarizeState.__annotations__)
    offenders = [k for k in keys if any(m in k.lower() for m in _STREAM_BUFFER_MARKERS)]
    assert offenders == [], f"SummarizeState must hold no stream buffer (ADR-0011): {offenders}"


def test_initial_state_has_no_stream_buffer() -> None:
    state = build_initial_state(correlation_id=_CID, request_id=_RID, lang="en")
    offenders = [k for k in state if any(m in k.lower() for m in _STREAM_BUFFER_MARKERS)]
    assert offenders == []
