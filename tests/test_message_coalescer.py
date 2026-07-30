"""Tests for :mod:`app.adapters.telegram.coalescer`.

The coalescer's job: buffer eligible consecutive Telegram messages from the
same chat, flush them after a 5 s idle window, and route them either through
the existing single-message path (1 buffered) or through the multi-source
aggregation handler (2+ buffered).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.telegram.coalescer import MessageCoalescer
from app.adapters.telegram.routing.content_router import MessageContentRouter
from app.adapters.telegram.routing.models import PreparedRouteContext
from app.adapters.telegram.routing.rate_limit import MessageRateLimitCoordinator
from app.application.dto.aggregation import SourceSubmission


def _prepared(
    *,
    uid: int = 42,
    chat_id: int = 999,
    text: str = "https://example.com/a",
    message_id: int = 1,
) -> PreparedRouteContext:
    """Minimal stand-in for ``PreparedRouteContext``."""
    return cast(
        "PreparedRouteContext",
        SimpleNamespace(
            uid=uid,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            correlation_id=f"cid-{message_id}",
            interaction_type="url",
        ),
    )


def _telethon_message(
    *,
    chat_id: int = 999,
    media_group_id: str | None = None,
    contact: Any = None,
    web_app_data: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        media_group_id=media_group_id,
        contact=contact,
        web_app_data=web_app_data,
        photo=None,
        document=None,
        forward_from_chat=None,
        forward_from=None,
        forward_sender_name=None,
    )


def _make_coalescer(
    *,
    enabled: bool = True,
    window_sec: float = 0.05,
    aggregation_handler: Any | None = None,
    callback_handler: Any | None = None,
    url_handler: Any | None = None,
    slot_available: bool = True,
) -> tuple[MessageCoalescer, dict[str, Any]]:
    """Build a coalescer with mock collaborators. Returns (coalescer, mocks)."""
    content_router = SimpleNamespace(route=AsyncMock())
    rate_limit_coordinator = SimpleNamespace(
        get_active_limiter=AsyncMock(return_value="limiter"),
        acquire_concurrent_slot=AsyncMock(return_value=slot_available),
        release_concurrent_slot=AsyncMock(),
        handle_concurrent_limit_rejection=AsyncMock(),
    )
    response_formatter = SimpleNamespace(send_chat_action=AsyncMock(return_value=True))
    coalescer = MessageCoalescer(
        window_sec=window_sec,
        enabled=enabled,
        content_router=cast("MessageContentRouter", content_router),
        aggregation_handler=aggregation_handler,
        rate_limit_coordinator=cast("MessageRateLimitCoordinator", rate_limit_coordinator),
        response_formatter=response_formatter,
        callback_handler=callback_handler,
        url_handler=url_handler,
        send_chat_action=response_formatter.send_chat_action,
    )
    return coalescer, {
        "content_router": content_router,
        "rate_limit_coordinator": rate_limit_coordinator,
        "response_formatter": response_formatter,
    }


def _make_aggregation_handler(
    *,
    enabled: bool = True,
    submissions_per_message: list[list[SourceSubmission]] | None = None,
) -> Any:
    submissions_per_message = submissions_per_message or []
    builds_iter = iter(submissions_per_message)

    async def _build(*, message: Any, text: str, include_message_source: bool | None = None):
        del message, text, include_message_source
        return next(builds_iter, [])

    return SimpleNamespace(
        ensure_enabled=AsyncMock(return_value=enabled),
        build_submissions_for_message=AsyncMock(side_effect=_build),
        run_with_submissions=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_single_message_flushes_through_content_router() -> None:
    coalescer, mocks = _make_coalescer()
    prepared = _prepared()
    message = _telethon_message()

    buffered = await coalescer.try_buffer(
        prepared=prepared,
        message=message,
        interaction_id=7,
        correlation_id="cid-1",
        start_time=100.0,
    )
    assert buffered is True

    await asyncio.sleep(0.15)

    mocks["content_router"].route.assert_awaited_once_with(prepared, 7, 100.0)
    mocks["rate_limit_coordinator"].acquire_concurrent_slot.assert_awaited_once()
    mocks["rate_limit_coordinator"].release_concurrent_slot.assert_awaited_once()


@pytest.mark.asyncio
async def test_refused_concurrency_slot_stops_the_flush() -> None:
    """A refused slot must abort the dispatch, not route anyway.

    This slot is the only enforcement of RATE_LIMIT_MAX_CONCURRENT on the
    coalesced path: message_router returns as soon as try_buffer accepts, before
    reaching its own inline check. The result used to be ignored, so the limit
    was unenforced for every non-command message and the user got no reply.
    """
    coalescer, mocks = _make_coalescer(slot_available=False)

    buffered = await coalescer.try_buffer(
        prepared=_prepared(),
        message=_telethon_message(),
        interaction_id=7,
        correlation_id="cid-1",
        start_time=100.0,
    )
    assert buffered is True

    await asyncio.sleep(0.15)

    mocks["content_router"].route.assert_not_awaited()
    mocks["rate_limit_coordinator"].handle_concurrent_limit_rejection.assert_awaited_once()
    kwargs = mocks["rate_limit_coordinator"].handle_concurrent_limit_rejection.await_args.kwargs
    assert kwargs["uid"] == 42
    assert kwargs["interaction_id"] == 7
    assert kwargs["correlation_id"] == "cid-1"
    # Nothing was acquired, so nothing may be released.
    mocks["rate_limit_coordinator"].release_concurrent_slot.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_messages_within_window_flush_as_bundle() -> None:
    sub_a = SourceSubmission.from_url("https://example.com/a")
    sub_b = SourceSubmission.from_url("https://example.com/b")
    handler = _make_aggregation_handler(submissions_per_message=[[sub_a], [sub_b]])
    coalescer, mocks = _make_coalescer(aggregation_handler=handler)

    await coalescer.try_buffer(
        prepared=_prepared(message_id=1, text="https://example.com/a"),
        message=_telethon_message(),
        interaction_id=10,
        correlation_id="cid-1",
        start_time=100.0,
    )
    await coalescer.try_buffer(
        prepared=_prepared(message_id=2, text="https://example.com/b"),
        message=_telethon_message(),
        interaction_id=11,
        correlation_id="cid-2",
        start_time=101.0,
    )

    await asyncio.sleep(0.15)

    handler.run_with_submissions.assert_awaited_once()
    kwargs = handler.run_with_submissions.await_args.kwargs
    assert [s.url for s in kwargs["submissions"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert kwargs["metadata"]["entrypoint"] == "telegram_coalesced"
    assert kwargs["metadata"]["buffered_message_ids"] == [1, 2]
    assert kwargs["metadata"]["per_message_correlation_ids"] == ["cid-1", "cid-2"]
    mocks["content_router"].route.assert_not_awaited()
    # One slot for the whole bundle
    mocks["rate_limit_coordinator"].acquire_concurrent_slot.assert_awaited_once()


@pytest.mark.asyncio
async def test_third_message_resets_timer() -> None:
    handler = _make_aggregation_handler(
        submissions_per_message=[
            [SourceSubmission.from_url(f"https://example.com/{c}")] for c in "abc"
        ]
    )
    coalescer, _ = _make_coalescer(aggregation_handler=handler, window_sec=0.1)

    for i in range(3):
        await coalescer.try_buffer(
            prepared=_prepared(message_id=i + 1, text=f"https://example.com/{chr(97 + i)}"),
            message=_telethon_message(),
            interaction_id=i,
            correlation_id=f"cid-{i + 1}",
            start_time=100.0 + i,
        )
        await asyncio.sleep(0.04)  # well under the 0.1s window

    # No flush yet — total elapsed since last append < 0.1s
    handler.run_with_submissions.assert_not_awaited()
    await asyncio.sleep(0.2)
    handler.run_with_submissions.assert_awaited_once()
    submissions = handler.run_with_submissions.await_args.kwargs["submissions"]
    assert len(submissions) == 3


@pytest.mark.asyncio
async def test_album_member_bypasses_coalescer() -> None:
    coalescer, _ = _make_coalescer()
    buffered = await coalescer.try_buffer(
        prepared=_prepared(),
        message=_telethon_message(media_group_id="album-1"),
        interaction_id=1,
        correlation_id="cid-1",
        start_time=100.0,
    )
    assert buffered is False


@pytest.mark.asyncio
async def test_command_text_bypasses_coalescer() -> None:
    coalescer, _ = _make_coalescer()
    buffered = await coalescer.try_buffer(
        prepared=_prepared(text="/help"),
        message=_telethon_message(),
        interaction_id=1,
        correlation_id="cid-1",
        start_time=100.0,
    )
    assert buffered is False


@pytest.mark.asyncio
async def test_pending_followup_bypasses_coalescer() -> None:
    callback_handler = SimpleNamespace(has_pending_followup=AsyncMock(return_value=True))
    coalescer, _ = _make_coalescer(callback_handler=callback_handler)
    buffered = await coalescer.try_buffer(
        prepared=_prepared(),
        message=_telethon_message(),
        interaction_id=1,
        correlation_id="cid-1",
        start_time=100.0,
    )
    assert buffered is False


@pytest.mark.asyncio
async def test_awaited_url_bypasses_coalescer() -> None:
    url_handler = SimpleNamespace(is_awaiting_url=AsyncMock(return_value=True))
    coalescer, _ = _make_coalescer(url_handler=url_handler)
    buffered = await coalescer.try_buffer(
        prepared=_prepared(),
        message=_telethon_message(),
        interaction_id=1,
        correlation_id="cid-1",
        start_time=100.0,
    )
    assert buffered is False


@pytest.mark.asyncio
async def test_contact_and_webapp_payloads_bypass() -> None:
    coalescer, _ = _make_coalescer()
    contact_buffered = await coalescer.try_buffer(
        prepared=_prepared(text=""),
        message=_telethon_message(contact=MagicMock()),
        interaction_id=1,
        correlation_id="cid-1",
        start_time=100.0,
    )
    web_app_buffered = await coalescer.try_buffer(
        prepared=_prepared(text=""),
        message=_telethon_message(web_app_data=MagicMock()),
        interaction_id=2,
        correlation_id="cid-2",
        start_time=100.0,
    )
    assert contact_buffered is False
    assert web_app_buffered is False


@pytest.mark.asyncio
async def test_disabled_coalescer_bypasses() -> None:
    coalescer, _ = _make_coalescer(enabled=False)
    buffered = await coalescer.try_buffer(
        prepared=_prepared(),
        message=_telethon_message(),
        interaction_id=1,
        correlation_id="cid-1",
        start_time=100.0,
    )
    assert buffered is False


@pytest.mark.asyncio
async def test_rollout_disabled_falls_back_to_per_message_dispatch() -> None:
    handler = _make_aggregation_handler(
        enabled=False,
        submissions_per_message=[[SourceSubmission.from_url("https://example.com/x")]],
    )
    coalescer, mocks = _make_coalescer(aggregation_handler=handler)

    prepared_a = _prepared(message_id=1, text="https://example.com/a")
    prepared_b = _prepared(message_id=2, text="https://example.com/b")
    await coalescer.try_buffer(
        prepared=prepared_a,
        message=_telethon_message(),
        interaction_id=10,
        correlation_id="cid-1",
        start_time=100.0,
    )
    await coalescer.try_buffer(
        prepared=prepared_b,
        message=_telethon_message(),
        interaction_id=11,
        correlation_id="cid-2",
        start_time=101.0,
    )
    await asyncio.sleep(0.2)

    handler.run_with_submissions.assert_not_awaited()
    # Both messages dispatched independently through the single-message path
    assert mocks["content_router"].route.await_count == 2


@pytest.mark.asyncio
async def test_flush_now_drains_immediately() -> None:
    coalescer, mocks = _make_coalescer(window_sec=10.0)
    prepared = _prepared()
    await coalescer.try_buffer(
        prepared=prepared,
        message=_telethon_message(),
        interaction_id=5,
        correlation_id="cid-1",
        start_time=100.0,
    )
    # No wait — call flush_now directly
    await coalescer.flush_now(prepared.uid, prepared.chat_id)
    mocks["content_router"].route.assert_awaited_once_with(prepared, 5, 100.0)


@pytest.mark.asyncio
async def test_flush_now_on_empty_bucket_is_safe() -> None:
    coalescer, mocks = _make_coalescer()
    await coalescer.flush_now(42, 999)
    mocks["content_router"].route.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_drains_open_buckets() -> None:
    coalescer, mocks = _make_coalescer(window_sec=10.0)
    await coalescer.try_buffer(
        prepared=_prepared(uid=1, chat_id=10, message_id=1),
        message=_telethon_message(chat_id=10),
        interaction_id=1,
        correlation_id="cid-1",
        start_time=100.0,
    )
    await coalescer.try_buffer(
        prepared=_prepared(uid=2, chat_id=20, message_id=2),
        message=_telethon_message(chat_id=20),
        interaction_id=2,
        correlation_id="cid-2",
        start_time=100.0,
    )

    await coalescer.shutdown()

    assert mocks["content_router"].route.await_count == 2

    # After shutdown, further attempts to buffer are rejected
    buffered = await coalescer.try_buffer(
        prepared=_prepared(),
        message=_telethon_message(),
        interaction_id=3,
        correlation_id="cid-3",
        start_time=100.0,
    )
    assert buffered is False


@pytest.mark.asyncio
async def test_typing_indicator_started_on_first_buffer() -> None:
    coalescer, mocks = _make_coalescer(window_sec=10.0)
    await coalescer.try_buffer(
        prepared=_prepared(),
        message=_telethon_message(),
        interaction_id=1,
        correlation_id="cid-1",
        start_time=100.0,
    )
    # TypingIndicator.start sends the action once synchronously
    mocks["response_formatter"].send_chat_action.assert_awaited()
    await coalescer.shutdown()
