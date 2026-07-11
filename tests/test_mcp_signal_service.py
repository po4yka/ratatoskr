from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.application.dto.request_workflow import RequestCreatedDTO
from app.core.time_utils import UTC
from app.mcp.signal_service import SignalMcpService


class _ScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _Session:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def scalars(self, _query: Any) -> _ScalarResult:
        return _ScalarResult(self._rows)


class _Database:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def session(self) -> _Session:
        return _Session(self._rows)


def _context(rows: list[Any], user_id: int | None = None) -> SimpleNamespace:
    runtime = SimpleNamespace(database=_Database(rows))
    return SimpleNamespace(user_id=user_id, ensure_runtime=lambda: runtime)


@pytest.mark.asyncio
async def test_list_sources_uses_runtime_database_session() -> None:
    source = SimpleNamespace(
        id=1,
        kind="rss",
        external_id="feed-1",
        url="https://example.com/feed.xml",
        title="Example",
        is_active=True,
        fetch_error_count=0,
        last_error=None,
    )

    payload = await SignalMcpService(_context([source])).list_sources()  # type: ignore[arg-type]

    assert payload == {
        "sources": [
            {
                "id": 1,
                "kind": "rss",
                "external_id": "feed-1",
                "url": "https://example.com/feed.xml",
                "title": "Example",
                "is_active": True,
                "fetch_error_count": 0,
                "last_error": None,
            }
        ]
    }


@pytest.mark.asyncio
async def test_list_signals_uses_runtime_database_session() -> None:
    source = SimpleNamespace(kind="rss", title="Example")
    feed_item = SimpleNamespace(title="Article", canonical_url="https://example.com", source=source)
    signal = SimpleNamespace(
        id=3,
        status="candidate",
        final_score=0.91,
        filter_stage="heuristic",
        feed_item=feed_item,
        topic_id=7,
        topic=SimpleNamespace(name="AI"),
    )

    payload = await SignalMcpService(_context([signal], user_id=42)).list_signals(  # type: ignore[arg-type]
        status="candidate"
    )

    assert payload == {
        "signals": [
            {
                "id": 3,
                "status": "candidate",
                "final_score": 0.91,
                "filter_stage": "heuristic",
                "title": "Article",
                "url": "https://example.com",
                "source_kind": "rss",
                "source_title": "Example",
                "topic_name": "AI",
            }
        ]
    }


@pytest.mark.asyncio
async def test_promote_queued_signal_creates_and_enqueues_a_durable_request() -> None:
    signal = SimpleNamespace(
        id=9,
        status="queued",
        user_id=42,
        feed_item=SimpleNamespace(canonical_url="https://example.com/article"),
    )

    class _PromotionSession:
        async def __aenter__(self) -> _PromotionSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, _query: Any) -> Any:
            return signal

        async def flush(self) -> None:
            return None

    request_service = SimpleNamespace(
        check_duplicate_url=AsyncMock(return_value=None),
        create_url_request=AsyncMock(
            return_value=RequestCreatedDTO(
                id=73,
                type="url",
                status="pending",
                correlation_id="promotion-73",
                created_at=datetime(2026, 7, 11, tzinfo=UTC),
            )
        ),
        mark_enqueue_failed=AsyncMock(),
    )
    durable_request_queue = SimpleNamespace(enqueue=AsyncMock())
    runtime = SimpleNamespace(
        db=SimpleNamespace(
            session=lambda: _PromotionSession(),
            transaction=lambda: _PromotionSession(),
        ),
        request_service=request_service,
        durable_request_queue=durable_request_queue,
    )
    context = SimpleNamespace(user_id=42, ensure_api_runtime=AsyncMock(return_value=runtime))

    payload = await SignalMcpService(context).promote_to_library("signal", 9)  # type: ignore[arg-type]

    assert payload == {
        "promoted": True,
        "source_type": "signal",
        "source_id": 9,
        "request_id": 73,
        "status": "queued",
        "duplicate": False,
    }
    request_service.check_duplicate_url.assert_awaited_once_with(42, "https://example.com/article")
    create_kwargs = request_service.create_url_request.await_args.kwargs
    assert create_kwargs["user_id"] == 42
    assert create_kwargs["input_url"] == "https://example.com/article"
    assert create_kwargs["correlation_id"].startswith("mcp-promotion-")
    durable_request_queue.enqueue.assert_awaited_once_with(
        request_id=73,
        correlation_id=create_kwargs["correlation_id"],
    )


@pytest.mark.asyncio
async def test_promote_x_bookmark_creates_a_user_owned_durable_request() -> None:
    bookmark = SimpleNamespace(
        id=19,
        type="x_bookmark",
        status="x_imported",
        user_id=None,
        input_url="https://x.com/example/status/1",
    )

    class _PromotionSession:
        async def __aenter__(self) -> _PromotionSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, _query: Any) -> Any:
            return bookmark

        async def flush(self) -> None:
            return None

    request_service = SimpleNamespace(
        check_duplicate_url=AsyncMock(return_value=None),
        create_url_request=AsyncMock(
            return_value=RequestCreatedDTO(
                id=74,
                type="url",
                status="pending",
                correlation_id="promotion-74",
                created_at=datetime(2026, 7, 11, tzinfo=UTC),
            )
        ),
        mark_enqueue_failed=AsyncMock(),
    )
    durable_request_queue = SimpleNamespace(enqueue=AsyncMock())
    runtime = SimpleNamespace(
        db=SimpleNamespace(
            session=lambda: _PromotionSession(),
            transaction=lambda: _PromotionSession(),
        ),
        request_service=request_service,
        durable_request_queue=durable_request_queue,
    )
    context = SimpleNamespace(user_id=42, ensure_api_runtime=AsyncMock(return_value=runtime))

    payload = await SignalMcpService(context).promote_to_library("x_bookmark", 19)  # type: ignore[arg-type]

    assert payload == {
        "promoted": True,
        "source_type": "x_bookmark",
        "source_id": 19,
        "request_id": 19,
        "status": "queued",
        "duplicate": False,
    }
    request_service.create_url_request.assert_not_awaited()
    enqueue_kwargs = durable_request_queue.enqueue.await_args.kwargs
    assert enqueue_kwargs["request_id"] == 19
    assert enqueue_kwargs["correlation_id"].startswith("mcp-promotion-")
    assert bookmark.user_id == 42
    assert bookmark.status == "pending"


@pytest.mark.asyncio
async def test_promotion_marks_request_error_when_durable_enqueue_fails() -> None:
    signal = SimpleNamespace(
        id=9,
        status="queued",
        user_id=42,
        feed_item=SimpleNamespace(canonical_url="https://example.com/article"),
    )

    class _PromotionSession:
        async def __aenter__(self) -> _PromotionSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, _query: Any) -> Any:
            return signal

    request_service = SimpleNamespace(
        check_duplicate_url=AsyncMock(return_value=None),
        create_url_request=AsyncMock(
            return_value=RequestCreatedDTO(
                id=75,
                type="url",
                status="pending",
                correlation_id="promotion-75",
                created_at=datetime(2026, 7, 11, tzinfo=UTC),
            )
        ),
        mark_enqueue_failed=AsyncMock(),
    )
    runtime = SimpleNamespace(
        db=SimpleNamespace(
            session=lambda: _PromotionSession(),
            transaction=lambda: _PromotionSession(),
        ),
        request_service=request_service,
        durable_request_queue=SimpleNamespace(enqueue=AsyncMock(side_effect=RuntimeError("down"))),
    )
    context = SimpleNamespace(user_id=42, ensure_api_runtime=AsyncMock(return_value=runtime))

    payload = await SignalMcpService(context).promote_to_library("signal", 9)  # type: ignore[arg-type]

    assert payload["error"].startswith("Unable to enqueue summary request. Error ID: ")
    assert payload["correlation_id"].startswith("mcp-promotion-")
    request_service.mark_enqueue_failed.assert_awaited_once_with(
        user_id=42,
        request_id=75,
        error_message=(
            "Unable to enqueue summary request. "
            f"Error ID: {payload['correlation_id']}"
        ),
    )
