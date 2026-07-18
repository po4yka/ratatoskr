"""Coverage for MessageRouter routing of forwarded messages."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

from app.adapters.telegram.message_router import MessageRouter
from tests.conftest import make_test_app_config

if TYPE_CHECKING:
    from app.db.session import Database


def _make_router(database: Database):
    cfg = make_test_app_config(db_path="/tmp/forward-routing.db")
    cfg = replace(
        cfg,
        runtime=cfg.runtime.model_copy(update={"aggregate_coalesce_enabled": False}),
    )

    command_processor = Mock()
    command_processor.has_active_init_session.return_value = False
    url_handler: Any = SimpleNamespace(
        url_processor=Mock(),
        is_awaiting_url=AsyncMock(return_value=False),
        handle_awaited_url=AsyncMock(),
        handle_direct_url=AsyncMock(),
        handle_document_file=AsyncMock(),
        can_handle_document=Mock(return_value=False),
        add_awaiting_user=AsyncMock(),
    )
    forward_processor: Any = SimpleNamespace(handle_forward_flow=AsyncMock())
    attachment_processor: Any = SimpleNamespace(handle_attachment_flow=AsyncMock())
    aggregation_handler: Any = SimpleNamespace(handle_message_bundle=AsyncMock())
    response_formatter: Any = SimpleNamespace(
        safe_reply=AsyncMock(),
        send_error_notification=AsyncMock(),
        # Spy on the typing indicator's underlying call so tests can assert
        # whether the router started a typing indicator for a given message.
        send_chat_action=AsyncMock(return_value=True),
    )

    router = MessageRouter(
        cfg=cfg,
        db=database,
        access_controller=SimpleNamespace(check_access=AsyncMock(return_value=True)),  # type: ignore[arg-type]
        command_processor=command_processor,
        url_handler=url_handler,
        forward_processor=forward_processor,
        attachment_processor=attachment_processor,
        aggregation_handler=aggregation_handler,
        response_formatter=response_formatter,
        audit_func=lambda *_args, **_kwargs: None,
    )
    return (
        router,
        forward_processor,
        attachment_processor,
        aggregation_handler,
        response_formatter,
        url_handler,
    )


def _base_message(**overrides: Any) -> SimpleNamespace:
    payload = {
        "id": 44,
        "chat": SimpleNamespace(id=9001),
        "from_user": SimpleNamespace(id=1, is_bot=False),
        "contact": None,
        "web_app_data": None,
        "document": None,
        "photo": None,
        "outgoing": False,
        "caption": None,
        "forward_from": None,
        "forward_from_chat": None,
        "forward_from_message_id": None,
        "forward_sender_name": None,
        "forward_date": None,
        "text": "",
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


async def test_forward_with_single_url_routes_to_forward_flow(database: Database) -> None:
    """A single-link forward is not a bare 'bundle' -- it goes to the forward
    flow (which link-enriches it), not the multi-source aggregation comparison.
    """
    (
        router,
        forward_processor,
        _attachment_processor,
        aggregation_handler,
        _response_formatter,
        url_handler,
    ) = _make_router(database)

    message = _base_message(
        text="https://example.com/article",
        forward_from_chat=SimpleNamespace(id=-100200300, title="Forwarded Channel"),
        forward_from_message_id=123,
        forward_date=1700000000,
    )

    await router.route_message(message)

    forward_processor.handle_forward_flow.assert_awaited_once()
    aggregation_handler.handle_message_bundle.assert_not_awaited()
    url_handler.handle_direct_url.assert_not_awaited()
    url_handler.handle_awaited_url.assert_not_awaited()


async def test_forward_from_user_with_text_routes_to_forward_flow(
    database: Database,
) -> None:
    (
        router,
        forward_processor,
        _attachment_processor,
        _aggregation_handler,
        _response_formatter,
        url_handler,
    ) = _make_router(database)

    message = _base_message(
        text="Some interesting article content",
        forward_from=SimpleNamespace(id=12345, first_name="John", last_name="Doe"),
        forward_date=1700000000,
    )

    await router.route_message(message)

    forward_processor.handle_forward_flow.assert_awaited_once()
    url_handler.handle_direct_url.assert_not_awaited()


async def test_forward_privacy_protected_with_text_routes_to_forward_flow(
    database: Database,
) -> None:
    (
        router,
        forward_processor,
        _attachment_processor,
        _aggregation_handler,
        _response_formatter,
        url_handler,
    ) = _make_router(database)

    message = _base_message(
        text="Privacy protected forward content",
        forward_sender_name="Hidden User",
        forward_date=1700000000,
    )

    await router.route_message(message)

    forward_processor.handle_forward_flow.assert_awaited_once()
    url_handler.handle_direct_url.assert_not_awaited()


async def test_forward_from_user_no_text_shows_error(database: Database) -> None:
    (
        router,
        forward_processor,
        _attachment_processor,
        _aggregation_handler,
        response_formatter,
        _url_handler,
    ) = _make_router(database)

    message = _base_message(
        text=None,
        forward_from=SimpleNamespace(id=12345, first_name="John", last_name="Doe"),
        forward_date=1700000000,
    )

    await router.route_message(message)

    forward_processor.handle_forward_flow.assert_not_awaited()
    response_formatter.safe_reply.assert_awaited_once()
    reply_text = response_formatter.safe_reply.call_args[0][1]
    assert "no text content" in reply_text.lower()


async def test_forwarded_channel_photo_with_caption_prefers_attachment_flow(
    database: Database,
) -> None:
    (
        router,
        forward_processor,
        attachment_processor,
        _aggregation_handler,
        _response_formatter,
        _url_handler,
    ) = _make_router(database)

    message = _base_message(
        caption="Forwarded photo caption",
        photo=[SimpleNamespace(file_id="photo-1")],
        forward_from_chat=SimpleNamespace(id=-100200300, title="Forwarded Channel"),
        forward_from_message_id=123,
        forward_date=1700000000,
    )

    await router.route_message(message)

    attachment_processor.handle_attachment_flow.assert_awaited_once()
    forward_processor.handle_forward_flow.assert_not_awaited()


async def test_forward_from_user_photo_with_caption_prefers_attachment_flow(
    database: Database,
) -> None:
    (
        router,
        forward_processor,
        attachment_processor,
        _aggregation_handler,
        _response_formatter,
        _url_handler,
    ) = _make_router(database)

    message = _base_message(
        caption="Forwarded photo caption",
        photo=[SimpleNamespace(file_id="photo-1")],
        forward_from=SimpleNamespace(id=12345, first_name="John", last_name="Doe"),
        forward_date=1700000000,
    )

    await router.route_message(message)

    attachment_processor.handle_attachment_flow.assert_awaited_once()
    forward_processor.handle_forward_flow.assert_not_awaited()


async def test_forward_message_with_multiple_urls_routes_via_aggregation_flow(
    database: Database,
) -> None:
    (
        router,
        forward_processor,
        attachment_processor,
        aggregation_handler,
        _response_formatter,
        url_handler,
    ) = _make_router(database)

    message = _base_message(
        text="https://example.com/a https://example.com/b",
        forward_from_chat=SimpleNamespace(id=-100200300, title="Forwarded Channel"),
        forward_from_message_id=123,
        forward_date=1700000000,
    )

    await router.route_message(message)

    aggregation_handler.handle_message_bundle.assert_awaited_once()
    forward_processor.handle_forward_flow.assert_not_awaited()
    attachment_processor.handle_attachment_flow.assert_not_awaited()
    url_handler.handle_direct_url.assert_not_awaited()


async def test_forward_substantive_post_with_links_routes_to_forward_flow(
    database: Database,
) -> None:
    """A forwarded post with substantive prose plus embedded links is summarized
    and link-enriched via the forward flow -- it is not a bare link bundle, so
    the multi-source aggregation comparison must not fire.
    """
    (
        router,
        forward_processor,
        _attachment_processor,
        aggregation_handler,
        _response_formatter,
        _url_handler,
    ) = _make_router(database)

    prose = "This is a substantive channel post with real analysis and context. " * 6
    message = _base_message(
        text=f"{prose} https://example.com/a https://example.com/b",
        forward_from_chat=SimpleNamespace(id=-100200300, title="Forwarded Channel"),
        forward_from_message_id=123,
        forward_date=1700000000,
    )

    await router.route_message(message)

    forward_processor.handle_forward_flow.assert_awaited_once()
    aggregation_handler.handle_message_bundle.assert_not_awaited()


# ===========================================================================
# Typing indicator: must fire the moment a content-bearing message arrives
# so the user sees feedback during scraping / enrichment / LLM cascade.
# ===========================================================================


async def test_forwarded_post_starts_typing_indicator_immediately(
    database: Database,
) -> None:
    (router, *_others, response_formatter, _url) = _make_router(database)

    message = _base_message(
        text="Channel post body with hyperlinked words",
        forward_from_chat=SimpleNamespace(id=-100200300, title="Forwarded Channel"),
        forward_from_message_id=123,
        forward_date=1700000000,
    )

    await router.route_message(message)

    # TypingIndicator.start() sends the first chat-action synchronously before
    # spawning the refresh task, so awaiting route_message must produce at
    # least one send_chat_action call before the handler returns.
    response_formatter.send_chat_action.assert_awaited()
    first_call = response_formatter.send_chat_action.await_args_list[0]
    assert first_call.args[1] == "typing"


async def test_url_message_starts_typing_indicator_immediately(
    database: Database,
) -> None:
    (router, *_others, response_formatter, _url) = _make_router(database)

    message = _base_message(text="https://example.com/article")

    await router.route_message(message)

    response_formatter.send_chat_action.assert_awaited()


async def test_command_does_not_start_typing_indicator(database: Database) -> None:
    (router, *_others, response_formatter, _url) = _make_router(database)

    message = _base_message(text="/start")

    await router.route_message(message)

    # Commands answer instantly -- no typing indicator should fire.
    response_formatter.send_chat_action.assert_not_awaited()


async def test_plain_text_does_not_start_typing_indicator(database: Database) -> None:
    (router, *_others, response_formatter, _url) = _make_router(database)

    message = _base_message(text="hello there")

    await router.route_message(message)

    # Plain text gets the fallback hint -- no typing indicator.
    response_formatter.send_chat_action.assert_not_awaited()
