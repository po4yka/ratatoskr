"""SSE endpoints for long-running operational tasks."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import orjson
from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from app.adapters.content.streaming.operation_streams import (
    OperationStreamEvent,
    get_operation_stream_hub,
    github_sync_topic,
    vector_reconcile_topic,
)
from app.api.routers.auth import get_current_user

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

HEARTBEAT_INTERVAL = 15
_TERMINAL_KINDS = frozenset({"done", "error"})
_STREAM_SCHEMA = {
    "type": "string",
    "description": (
        "Server-Sent Events stream. Each event data field is JSON with "
        "kind, payload, timestamp, and correlation_id fields."
    ),
}
GITHUB_SYNC_STREAM_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": (
            "GitHub sync progress stream. Events: phase, repos_fetched, "
            "repos_analyzed, done, error."
        ),
        "content": {"text/event-stream": {"schema": _STREAM_SCHEMA}},
    }
}
VECTOR_RECONCILE_STREAM_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": (
            "Vector reconciler progress stream. Events: phase, rows_scanned, "
            "rows_requeued, done, error."
        ),
        "content": {"text/event-stream": {"schema": _STREAM_SCHEMA}},
    }
}
DIGEST_RUN_STREAM_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": (
            "Digest run progress stream. Events: phase, channel_processed, "
            "posts_analyzed, delivered, done, error."
        ),
        "content": {"text/event-stream": {"schema": _STREAM_SCHEMA}},
    }
}

router = APIRouter()


@router.get(
    "/github/syncs/{sync_id}/stream",
    response_class=EventSourceResponse,
    responses=GITHUB_SYNC_STREAM_RESPONSES,
)
async def stream_github_sync(
    sync_id: str,
    _user: dict[str, Any] = Depends(get_current_user),
) -> EventSourceResponse:
    """Stream progress for a manual GitHub sync run."""
    return _event_source_response(github_sync_topic(sync_id))


@router.get(
    "/vector-reconciler/runs/{run_id}/stream",
    response_class=EventSourceResponse,
    responses=VECTOR_RECONCILE_STREAM_RESPONSES,
)
async def stream_vector_reconcile(
    run_id: str,
    _user: dict[str, Any] = Depends(get_current_user),
) -> EventSourceResponse:
    """Stream progress for a vector-reconciler run."""
    return _event_source_response(vector_reconcile_topic(run_id))


def event_source_for_operation_topic(topic: str) -> EventSourceResponse:
    """Build an SSE response for a shared operation-stream topic."""
    return _event_source_response(topic)


def _event_source_response(topic: str) -> EventSourceResponse:
    hub = get_operation_stream_hub()

    async def event_generator() -> AsyncIterator[dict[str, Any]]:
        try:
            async for event in hub.subscribe(topic):
                yield _to_sse_event(event)
                if event.kind in _TERMINAL_KINDS:
                    return
        except (asyncio.CancelledError, GeneratorExit):
            return

    return EventSourceResponse(
        event_generator(),
        ping=HEARTBEAT_INTERVAL,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _to_sse_event(event: OperationStreamEvent) -> dict[str, Any]:
    return {
        "event": event.kind,
        "data": orjson.dumps(
            {
                "kind": event.kind,
                "payload": event.payload,
                "timestamp": event.timestamp.isoformat(),
                "correlation_id": event.correlation_id,
            }
        ).decode(),
    }
