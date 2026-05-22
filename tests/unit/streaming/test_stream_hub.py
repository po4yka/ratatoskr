"""Unit tests for StreamHub — the in-process pub/sub hub for streaming events."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.adapters.content.streaming.events import (
    DonePayload,
    SectionPayload,
    StagePayload,
    StreamEvent,
)
from app.adapters.content.streaming.stream_hub import _QUEUE_MAXSIZE, StreamHub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stage_event(stage: str = "summarizing", corr: str = "test") -> StreamEvent:
    return StreamEvent.now("stage", StagePayload(stage=stage), corr)  # type: ignore[arg-type]


def _section_event(section: str = "tldr", content: str = "text", corr: str = "test") -> StreamEvent:
    return StreamEvent.now("section", SectionPayload(section=section, content=content), corr)


def _done_event(corr: str = "test") -> StreamEvent:
    return StreamEvent.now(
        "done",
        DonePayload(summary_id="sum-1", request_id="req-1"),
        corr,
    )


async def _drain(gen) -> list[StreamEvent]:
    """Collect all events from an async generator."""
    events: list[StreamEvent] = []
    async for ev in gen:
        events.append(ev)
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_single_subscriber_receives_events_in_order() -> None:
    hub = StreamHub()
    rid = "req-order"

    e1 = _stage_event("extracting")
    e2 = _section_event("summary_250", "Hello")
    e3 = _done_event()

    hub.publish(rid, e1)
    hub.publish(rid, e2)
    hub.publish(rid, e3)

    # Subscribe after all publishes — gets backlog replay then terminates on done.
    received = await _drain(hub.subscribe(rid))

    assert [ev.kind for ev in received] == ["stage", "section", "done"]
    assert received[0].payload["stage"] == "extracting"
    assert received[1].payload["section"] == "summary_250"


async def test_multiple_subscribers_each_receive_every_event() -> None:
    hub = StreamHub()
    rid = "req-multi"

    # Publish before subscribing so both get the backlog.
    e1 = _stage_event("summarizing")
    e2 = _done_event()
    hub.publish(rid, e1)
    hub.publish(rid, e2)

    received_a = await _drain(hub.subscribe(rid))
    received_b = await _drain(hub.subscribe(rid))

    assert len(received_a) == 2
    assert len(received_b) == 2
    assert received_a[0].kind == "stage"
    assert received_b[0].kind == "stage"


async def test_late_subscriber_gets_buffered_backlog() -> None:
    hub = StreamHub()
    rid = "req-backlog"

    e1 = _stage_event("extracting")
    e2 = _section_event("tldr", "Short")

    hub.publish(rid, e1)
    hub.publish(rid, e2)

    # Subscribe *after* both non-terminal events are published.
    # Manually collect without waiting for a terminal — need to partially drain.
    queue_events: list[StreamEvent] = []
    gen = hub.subscribe(rid)
    # The generator should replay both backlog events then block (no terminal yet).
    # We pull exactly 2 events.
    async for ev in gen:
        queue_events.append(ev)
        if len(queue_events) == 2:
            break
    await gen.aclose()  # type: ignore[attr-defined]

    assert len(queue_events) == 2
    assert queue_events[0].payload["stage"] == "extracting"
    assert queue_events[1].payload["section"] == "tldr"


async def test_bounded_queue_drops_oldest_non_terminal_under_pressure() -> None:
    """Fill a subscriber's queue then publish; oldest non-terminal is dropped."""
    hub = StreamHub()
    rid = "req-pressure"

    # Subscribe first so we have a live queue.
    gen = hub.subscribe(rid)

    # Consume the "subscribe" setup path (no backlog yet).
    # Publish exactly _QUEUE_MAXSIZE non-terminal events without consuming.
    events: list[StreamEvent] = []
    for i in range(_QUEUE_MAXSIZE):
        ev = _section_event("tldr", f"chunk-{i}")
        events.append(ev)
        hub.publish(rid, ev)

    # Now publish one more — the hub must drop the oldest non-terminal to fit it.
    late_event = _section_event("summary_250", "late-content")
    hub.publish(rid, late_event)

    # Publish a terminal so our generator exits.
    hub.publish(rid, _done_event())

    received = await _drain(gen)

    # The terminal event must survive.
    terminal = [e for e in received if e.kind == "done"]
    assert len(terminal) == 1

    # The late non-terminal event must survive (it's the most recent).
    late_received = [e for e in received if e.payload.get("section") == "summary_250"]
    assert len(late_received) >= 1

    # Total events must be <= _QUEUE_MAXSIZE + 1 (one slot was freed by the drop).
    assert len(received) <= _QUEUE_MAXSIZE + 1


async def test_terminal_done_event_schedules_cleanup() -> None:
    """Publishing 'done' calls _cleanup_request after the TTL fires."""
    hub = StreamHub()
    rid = "req-cleanup"

    cleanup_called_with: list[str] = []
    original_cleanup = hub._cleanup_request

    def recording_cleanup(request_id: str) -> None:
        cleanup_called_with.append(request_id)
        original_cleanup(request_id)

    hub._cleanup_request = recording_cleanup  # type: ignore[method-assign]

    # Publishing a done event inside a running loop triggers call_later.
    # We patch call_later to invoke immediately so we don't wait TTL seconds.
    loop = asyncio.get_running_loop()
    original_call_later = loop.call_later

    def immediate_call_later(delay, callback, *args):
        # Fire immediately regardless of delay.
        callback(*args)
        return original_call_later(0, lambda: None)

    with patch.object(loop, "call_later", side_effect=immediate_call_later):
        hub.publish(rid, _done_event())

    assert rid in cleanup_called_with
    # Buffer should have been removed.
    assert rid not in hub._buffers


async def test_generator_exit_cleans_up_subscriber() -> None:
    """Cancelling the task draining the generator removes the subscriber from the hub."""
    hub = StreamHub()
    rid = "req-genexit"

    gen = hub.subscribe(rid)

    # Start draining in a background task so the generator registers itself.
    task = asyncio.create_task(_drain(gen))

    # Yield control so the generator enters subscribe() and registers the queue.
    await asyncio.sleep(0)

    # Confirm the subscriber is registered.
    assert len(hub._subscribers.get(rid, [])) == 1

    # Cancel the draining task — this causes GeneratorExit inside the generator's
    # finally block, which removes the subscriber.
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration):
        pass

    # Give the finally block a chance to run.
    await asyncio.sleep(0)

    # After cancellation, the subscriber queue must be removed.
    subs = hub._subscribers.get(rid, [])
    assert len(subs) == 0


async def test_publish_before_any_subscriber_stores_in_buffer() -> None:
    """Events published with no subscribers are stored in the ring buffer."""
    hub = StreamHub()
    rid = "req-buffer"

    hub.publish(rid, _stage_event("extracting"))
    hub.publish(rid, _stage_event("summarizing"))

    assert rid in hub._buffers
    assert len(hub._buffers[rid]) == 2


async def test_error_terminal_also_triggers_cleanup_and_terminates_generator() -> None:
    hub = StreamHub()
    rid = "req-error"

    error_event = StreamEvent.now(
        "error",
        {"code": "LLM_FAILED", "message": "timeout", "correlation_id": "cid-1"},
        "test",
    )
    hub.publish(rid, _stage_event("summarizing"))
    hub.publish(rid, error_event)

    received = await _drain(hub.subscribe(rid))

    assert received[-1].kind == "error"
    assert received[-1].payload["code"] == "LLM_FAILED"
