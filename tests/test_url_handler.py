import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.telegram.url_handler import URLHandler

if TYPE_CHECKING:
    from app.adapters.content.graph_url_processor import GraphURLProcessor as URLProcessor
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.db.session import DatabaseSessionManager  # type: ignore[attr-defined]
else:  # pragma: no cover - runtime fallback for typing-only imports
    URLProcessor = ResponseFormatter = DatabaseSessionManager = Any  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_handle_awaited_url_rejects_invalid_links() -> None:
    safe_reply_mock = AsyncMock()
    response_formatter = cast(
        "ResponseFormatter",
        SimpleNamespace(
            MAX_BATCH_URLS=5,
            safe_reply=safe_reply_mock,
            send_error_notification=AsyncMock(),
            _validate_url=MagicMock(return_value=(False, "bad")),
        ),
    )
    handle_url_flow_mock = AsyncMock()
    url_processor = cast(
        "URLProcessor",
        SimpleNamespace(handle_url_flow=handle_url_flow_mock, summary_repo=None, audit_func=None),
    )
    handler = URLHandler(
        db=cast("DatabaseSessionManager", SimpleNamespace()),
        response_formatter=response_formatter,
        url_processor=url_processor,
    )

    message = SimpleNamespace(chat=None)

    await handler.handle_awaited_url(
        message,
        "https://localhost/resource",
        uid=99,
        correlation_id="cid",
        interaction_id=0,
        start_time=0.0,
    )

    # Use cast to Any to satisfy mypy for AsyncMock attributes
    assert cast("Any", response_formatter.send_error_notification).await_count == 1
    assert safe_reply_mock.await_count == 0
    assert handle_url_flow_mock.await_count == 0


@pytest.mark.asyncio
async def test_handle_awaited_url_filters_invalid_before_processing() -> None:
    safe_reply_mock = AsyncMock()
    response_formatter = cast(
        "ResponseFormatter",
        SimpleNamespace(
            MAX_BATCH_URLS=5,
            safe_reply=safe_reply_mock,
            send_error_notification=AsyncMock(),
            _validate_url=MagicMock(side_effect=[(True, ""), (False, "bad")]),
        ),
    )
    handle_url_flow_mock = AsyncMock()
    url_processor = cast(
        "URLProcessor",
        SimpleNamespace(handle_url_flow=handle_url_flow_mock, summary_repo=None, audit_func=None),
    )
    handler = URLHandler(
        db=cast("DatabaseSessionManager", SimpleNamespace()),
        response_formatter=response_formatter,
        url_processor=url_processor,
    )
    message = SimpleNamespace(chat=None)

    await handler.handle_awaited_url(
        message,
        "https://valid.example/path https://localhost/resource",
        uid=42,
        correlation_id="cid",
        interaction_id=0,
        start_time=0.0,
    )

    assert handle_url_flow_mock.await_count == 1


@pytest.mark.asyncio
async def test_handle_single_url_delegates_to_processor() -> None:
    handle_url_flow_mock = AsyncMock(return_value="ok")
    url_processor = cast(
        "URLProcessor",
        SimpleNamespace(handle_url_flow=handle_url_flow_mock, summary_repo=None, audit_func=None),
    )
    handler = URLHandler(
        db=cast("DatabaseSessionManager", SimpleNamespace()),
        response_formatter=cast(
            "ResponseFormatter",
            SimpleNamespace(
                MAX_BATCH_URLS=5,
                safe_reply=AsyncMock(),
                send_error_notification=AsyncMock(),
            ),
        ),
        url_processor=url_processor,
    )

    result = await handler.handle_single_url(
        message=SimpleNamespace(chat=None),
        url="https://example.com",
        correlation_id="cid",
        interaction_id=7,
    )

    assert result == "ok"
    handle_url_flow_mock.assert_awaited_once()


def _make_handler() -> URLHandler:
    """Create a minimal URLHandler for state-management tests."""
    return URLHandler(
        db=cast("DatabaseSessionManager", SimpleNamespace()),
        response_formatter=cast(
            "ResponseFormatter",
            SimpleNamespace(
                MAX_BATCH_URLS=5, safe_reply=AsyncMock(), send_error_notification=AsyncMock()
            ),
        ),
        url_processor=cast(
            "URLProcessor",
            SimpleNamespace(
                summary_repo=None,
                audit_func=None,
                handle_url_flow=AsyncMock(),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_awaiting_users_expire_after_ttl() -> None:
    """Users in _awaiting_url_users should expire after TTL."""
    handler = _make_handler()

    await handler.add_awaiting_user(100)
    assert await handler.is_awaiting_url(100)

    # Simulate time passing beyond TTL (default 120s)
    with patch("app.adapters.telegram.url_state_store.time") as mock_time:
        mock_time.time.return_value = time.time() + 130
        assert not await handler.is_awaiting_url(100), "Should have expired"


@pytest.mark.asyncio
async def test_cleanup_expired_state() -> None:
    """cleanup_expired_state removes stale entries."""
    handler = _make_handler()

    await handler.add_awaiting_user(100)

    with patch("app.adapters.telegram.url_state_store.time") as mock_time:
        mock_time.time.return_value = time.time() + 130
        cleaned = await handler.cleanup_expired_state()
        assert cleaned == 1
