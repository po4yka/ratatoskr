"""A cancelled streamed summarize must still close its stream.

``StreamHub`` frees a request's ring buffer and subscriber list only when a
terminal event is published, and it has no independent TTL. The streamed runner
documents that it emits exactly one terminal ``done``/``error`` before returning,
but its ``except Exception`` could not see ``CancelledError`` -- so a cancelled
run (Telegram ``/cancel``, or teardown of whatever drives it) published neither,
stranding every SSE subscriber on ``queue.get()`` and leaking that request's hub
state for the life of the process.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.content.streaming.stream_hub import StreamHub
from app.application.graphs.summarize.lifecycle import REASON_GRAPH_CANCELLED
from app.di.graphs import run_summarize_graph_streamed


class _RecordingSink:
    def __init__(self) -> None:
        self.terminal: list[tuple[str, dict[str, Any]]] = []

    async def progress(self, **_kwargs: Any) -> None:
        return None

    async def token(self, **_kwargs: Any) -> None:
        return None

    async def done(self, **kwargs: Any) -> None:
        self.terminal.append(("done", kwargs))

    async def error(self, **kwargs: Any) -> None:
        self.terminal.append(("error", kwargs))


class _HangingGraph:
    """Stands in for the compiled graph: starts streaming, then never finishes."""

    def __init__(self, entered: asyncio.Event) -> None:
        self._entered = entered

    async def astream_events(self, _graph_input: Any, **_kwargs: Any) -> Any:
        self._entered.set()
        await asyncio.Event().wait()
        yield {}  # pragma: no cover -- unreachable; keeps this an async generator


@pytest.fixture
def _stub_graph_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the checkpoint helpers the runner calls around the stream."""
    from app.di import graphs

    async def _prepare(_graph: Any, _config: Any, initial_state: Any) -> tuple[Any, None]:
        return initial_state, None

    monkeypatch.setattr(graphs, "prepare_resumable_invocation", _prepare)
    monkeypatch.setattr(graphs, "GraphEventBridge", None, raising=False)


async def _run_and_cancel(sink: _RecordingSink, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.di import graphs

    entered = asyncio.Event()

    async def _prepare(_graph: Any, _config: Any, initial_state: Any) -> tuple[Any, None]:
        return initial_state, None

    monkeypatch.setattr(graphs, "prepare_resumable_invocation", _prepare)

    task = asyncio.create_task(
        run_summarize_graph_streamed(
            graph=_HangingGraph(entered),
            deps=SimpleNamespace(),  # type: ignore[arg-type]
            sink=sink,  # type: ignore[arg-type]
            correlation_id="cid-1",
            request_id=42,
            lang="en",
            input_url="https://example.com/article",
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancelled_run_publishes_a_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = _RecordingSink()

    await _run_and_cancel(sink, monkeypatch)

    assert sink.terminal, (
        "the cancelled run published no terminal event, so SSE subscribers wait "
        "forever and the hub never frees this request's state"
    )
    kind, payload = sink.terminal[0]
    assert kind == "error"
    assert payload["code"] == REASON_GRAPH_CANCELLED
    assert payload["request_id"] == "42"


async def test_exactly_one_terminal_event_is_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner's documented invariant must survive the new path."""
    sink = _RecordingSink()

    await _run_and_cancel(sink, monkeypatch)

    assert len(sink.terminal) == 1


async def test_a_terminal_event_lets_the_hub_free_the_request() -> None:
    """Why the event matters: cleanup is keyed on terminal kinds alone."""
    from app.adapters.content.streaming.events import StreamEvent

    from datetime import UTC, datetime

    def _event(kind: str) -> StreamEvent:
        return StreamEvent(
            kind=kind,  # type: ignore[arg-type]
            payload={},
            timestamp=datetime.now(UTC),
            correlation_id="cid-1",
        )

    hub = StreamHub()
    hub.publish("42", _event("progress"))
    assert "42" in hub._buffers

    hub.publish("42", _event("error"))
    hub._cleanup_request("42")

    assert "42" not in hub._buffers
    assert "42" not in hub._subscribers
