"""Server-Sent Events endpoint for streaming summary progress."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import orjson
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request as FastAPIRequest,
    status,
)
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.adapters.content.streaming import get_stream_hub
from app.api.routers.auth import get_current_user
from app.application.services.request_service import RequestService
from app.domain.exceptions.domain_exceptions import (
    ResourceNotFoundError as DomainResourceNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter()

HEARTBEAT_INTERVAL = 15  # seconds


def _get_request_service(request: FastAPIRequest) -> RequestService:
    """Resolve the shared request workflow service from API runtime."""
    import contextlib

    with contextlib.suppress(RuntimeError):
        from app.di.api import resolve_api_runtime

        return cast("RequestService", resolve_api_runtime(request).request_service)

    from app.api.dependencies.database import (
        get_crawl_result_repository,
        get_llm_repository,
        get_request_repository,
        get_session_manager,
        get_summary_repository,
    )

    db = get_session_manager(request)
    return RequestService(
        db=db,
        request_repository=get_request_repository(db, request),
        summary_repository=get_summary_repository(db, request),
        crawl_result_repository=get_crawl_result_repository(db, request),
        llm_repository=get_llm_repository(db, request),
    )


def _get_progress_event_repository(request: FastAPIRequest) -> Any | None:
    import contextlib

    with contextlib.suppress(RuntimeError):
        from app.di.api import resolve_api_runtime

        return resolve_api_runtime(request).progress_event_repository
    return None


@router.get("/{request_id}/stream")
async def stream_request(
    request_id: int,
    fastapi_request: FastAPIRequest,
    since_sequence: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    user: dict[str, Any] = Depends(get_current_user),
    request_service: RequestService = Depends(_get_request_service),
    progress_event_repo: Any | None = Depends(_get_progress_event_repository),
) -> EventSourceResponse:
    """Stream processing events for a specific request via SSE.

    Replays buffered events first (ring-buffer backlog), then delivers live
    events until a terminal ``done`` or ``error`` event is received.  The
    connection is kept alive with automatic SSE comment-line heartbeats every
    ``HEARTBEAT_INTERVAL`` seconds.

    Returns 403 when the request does not belong to the authenticated user.
    """
    # Load the Request row and verify ownership — mirror get_request pattern.
    try:
        details = await request_service.get_request_by_id(user["user_id"], request_id)
    except Exception as exc:
        if isinstance(exc, DomainResourceNotFoundError):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Request not found or access denied",
            ) from exc
        logger.bind(request_id=str(request_id)).error(
            "stream.error",
            error=repr(exc),
        )
        try:
            import sentry_sdk

            sentry_sdk.add_breadcrumb(
                category="stream",
                level="error",
                data={"request_id": request_id, "error": repr(exc)},
            )
        except ImportError:
            pass
        raise

    del details  # ownership confirmed; we don't need the full details object

    if progress_event_repo is None:
        return _stream_from_local_hub(request_id)

    async def event_generator() -> AsyncIterator[dict[str, Any]]:
        sequence = since_sequence
        if last_event_id:
            stored_sequence = await progress_event_repo.sequence_for_event_id(
                request_id=request_id,
                event_id=last_event_id,
            )
            if stored_sequence is not None:
                sequence = max(sequence, stored_sequence)
        try:
            while True:
                events = await progress_event_repo.list_after_sequence(
                    request_id=request_id,
                    sequence=sequence,
                    limit=100,
                )
                for event in events:
                    sequence = event.sequence
                    yield _to_sse_event(event)
                    if event.kind in ("done", "error"):
                        return
                if await fastapi_request.is_disconnected():
                    return
                await asyncio.sleep(0.5)
        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnect: stop iterating; the underlying summarization continues.
            return

    return _event_source_response(event_generator())


def _stream_from_local_hub(request_id: int) -> EventSourceResponse:
    hub = get_stream_hub()

    async def event_generator() -> AsyncIterator[dict[str, Any]]:
        subscription = hub.subscribe(str(request_id))
        try:
            async for event in subscription:
                yield {
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
                if event.kind in ("done", "error"):
                    return
        except (asyncio.CancelledError, GeneratorExit):
            return

    return _event_source_response(event_generator())


def _to_sse_event(event: Any) -> dict[str, Any]:
    payload = event.as_sse_payload()
    return {
        "id": event.event_id,
        "event": event.kind,
        "data": orjson.dumps(payload).decode(),
    }


def _event_source_response(event_generator: AsyncIterator[dict[str, Any]]) -> EventSourceResponse:
    return EventSourceResponse(
        event_generator,
        ping=HEARTBEAT_INTERVAL,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
