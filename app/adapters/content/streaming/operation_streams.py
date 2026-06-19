"""In-process pub/sub for long-running operation progress streams."""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_RING_BUFFER_MAXLEN = 1
_QUEUE_MAXSIZE = 128
_CLEANUP_TTL_SECONDS = 60
_TERMINAL_KINDS = frozenset({"done", "error"})


@dataclass(frozen=True, slots=True)
class OperationStreamEvent:
    """A domain-specific operation progress event."""

    kind: str
    payload: dict[str, Any]
    timestamp: datetime
    correlation_id: str

    @classmethod
    def now(
        cls,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        correlation_id: str,
    ) -> OperationStreamEvent:
        return cls(
            kind=kind,
            payload=payload or {},
            timestamp=datetime.now(UTC),
            correlation_id=correlation_id,
        )


class OperationStreamHub:
    """Small in-process pub/sub hub with one-event replay for reconnects."""

    def __init__(self) -> None:
        self._buffers: dict[str, deque[OperationStreamEvent]] = {}
        self._subscribers: dict[str, list[asyncio.Queue[OperationStreamEvent]]] = {}
        self._lock = asyncio.Lock()

    def publish(self, topic: str, event: OperationStreamEvent) -> None:
        if topic not in self._buffers:
            self._buffers[topic] = deque(maxlen=_RING_BUFFER_MAXLEN)
        self._buffers[topic].append(event)

        for queue in self._subscribers.get(topic, []):
            _put_event(queue, event)

        if event.kind in _TERMINAL_KINDS:
            self._schedule_cleanup(topic)

    async def subscribe(self, topic: str) -> AsyncIterator[OperationStreamEvent]:
        queue: asyncio.Queue[OperationStreamEvent] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        async with self._lock:
            backlog = list(self._buffers.get(topic, []))
            self._subscribers.setdefault(topic, []).append(queue)

        try:
            for event in backlog:
                yield event
                if event.kind in _TERMINAL_KINDS:
                    return

            while True:
                event = await queue.get()
                yield event
                if event.kind in _TERMINAL_KINDS:
                    return
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(topic)
                if subscribers is not None and queue in subscribers:
                    subscribers.remove(queue)

    def _schedule_cleanup(self, topic: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._cleanup_topic(topic)
            return
        loop.call_later(_CLEANUP_TTL_SECONDS, self._cleanup_topic, topic)

    def _cleanup_topic(self, topic: str) -> None:
        self._buffers.pop(topic, None)
        self._subscribers.pop(topic, None)


def publish_operation_event(
    *,
    topic: str,
    kind: str,
    correlation_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Publish an operation progress event to the process-local hub."""
    get_operation_stream_hub().publish(
        topic,
        OperationStreamEvent.now(kind=kind, payload=payload, correlation_id=correlation_id),
    )


def digest_run_topic(run_id: str) -> str:
    return f"digest:{run_id}"


def github_sync_topic(sync_id: str) -> str:
    return f"github-sync:{sync_id}"


def vector_reconcile_topic(run_id: str) -> str:
    return f"vector-reconcile:{run_id}"


def get_operation_stream_hub() -> OperationStreamHub:
    global _hub
    if _hub is None:
        _hub = OperationStreamHub()
    return _hub


def _put_event(
    queue: asyncio.Queue[OperationStreamEvent],
    event: OperationStreamEvent,
) -> None:
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(event)


_hub: OperationStreamHub | None = None

__all__ = [
    "OperationStreamEvent",
    "OperationStreamHub",
    "digest_run_topic",
    "get_operation_stream_hub",
    "github_sync_topic",
    "publish_operation_event",
    "vector_reconcile_topic",
]
