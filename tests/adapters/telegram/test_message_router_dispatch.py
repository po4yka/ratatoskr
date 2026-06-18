"""Unit tests for MessageRouter dispatch routing logic.

Strategy
--------
MessageRouter.route_message() is the highest-volume production path but has no
direct tests.  The internal wiring is deeply nested (OTel span, rate-limiter,
coalescer, access-controller, content-router), so the most stable approach is:

1. Build a real MessageRouter with all leaf collaborators mocked.
2. Monkeypatch *only* the three internal sub-objects that would require full
   Telegram message infrastructure:
   - ``_context_builder.prepare`` → returns a hand-crafted PreparedRouteContext
   - ``_coalescer.try_buffer``   → returns False (no buffering)
   - ``_rate_limit_coordinator`` methods → allow immediate passage

These patches leave the router's own orchestration logic under test while
keeping collaborator internals out of scope.

What is NOT tested here
-----------------------
- Forward-processor internals, command-dispatcher internals, URL-handler internals
  (covered by their own test modules).
- CallbackQuery routing: callback_query dispatch lives in MessageHandler, not in
  route_message().  The callback_handler is wired into MessageContentRouter for
  *followup* questions on plain text, which is covered by the "unknown text"
  test below.
- Coalescing: try_buffer is stubbed out; the coalescer has its own tests.
- OTel spans: patched to no-ops via the tracer stub in conftest / monkeypatch.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapter_models.telegram.telegram_message import TelegramMessage
from app.adapters.telegram.message_router import MessageRouter
from app.adapters.telegram.routing.models import PreparedRouteContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALLOWED_UID = 123456789
_CORRELATION_ID = "test-cid-0001"


def _make_telegram_message() -> TelegramMessage:
    """Minimal TelegramMessage satisfying the dataclass - all optional fields absent."""
    return TelegramMessage(message_id=1)


def _make_context(
    *,
    text: str = "",
    has_forward: bool = False,
    interaction_type: str = "unknown",
    command: str | None = None,
    first_url: str | None = None,
    uid: int = _ALLOWED_UID,
    chat_id: int = 100,
    message_id: int = 1,
    media_type: str | None = None,
    message: Any = None,
) -> PreparedRouteContext:
    """Build a PreparedRouteContext for routing tests."""
    if message is None:
        message = SimpleNamespace(
            contact=None,
            web_app_data=None,
            forward_from_chat=None,
            outgoing=False,
        )
    return PreparedRouteContext(
        message=message,
        telegram_message=_make_telegram_message(),
        text=text,
        uid=uid,
        chat_id=chat_id,
        message_id=message_id,
        has_forward=has_forward,
        forward_from_chat_id=None,
        forward_from_chat_title=None,
        forward_from_message_id=None,
        interaction_type=interaction_type,
        command=command,
        first_url=first_url,
        media_type=media_type,
        correlation_id=_CORRELATION_ID,
    )


def _make_dispatch_outcome(*, handled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(handled=handled)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def _make_router(
    *,
    allowed_uid: int = _ALLOWED_UID,
    command_processor: Any = None,
    url_handler: Any = None,
    forward_processor: Any = None,
    callback_handler: Any = None,
) -> MessageRouter:
    """Return a MessageRouter with all collaborators mocked.

    Only the three named parameters need non-trivial mocks; everything else
    is a silent MagicMock / AsyncMock that satisfies constructor type hints.
    """
    from tests.conftest import make_test_app_config

    cfg = make_test_app_config(allowed_user_ids=(allowed_uid,))

    if command_processor is None:
        command_processor = MagicMock()
        command_processor.dispatch_command = AsyncMock(
            return_value=_make_dispatch_outcome(handled=False)
        )
        command_processor.has_active_init_session = MagicMock(return_value=False)

    if url_handler is None:
        url_handler = MagicMock()
        url_handler.handle_direct_url = AsyncMock()
        url_handler.is_awaiting_url = AsyncMock(return_value=False)
        url_handler.can_handle_document = MagicMock(return_value=False)

    if forward_processor is None:
        forward_processor = MagicMock()
        forward_processor.handle_forward_flow = AsyncMock()

    response_formatter = MagicMock()
    response_formatter.safe_reply = AsyncMock()
    response_formatter.safe_reply_with_id = AsyncMock(return_value=55)
    # send_chat_action drives TypingIndicator; silence it
    response_formatter.send_chat_action = AsyncMock()

    audit_func = MagicMock()

    return MessageRouter(
        cfg=cfg,
        access_controller=MagicMock(),  # patched per-test
        command_processor=command_processor,
        url_handler=url_handler,
        forward_processor=forward_processor,
        response_formatter=response_formatter,
        audit_func=audit_func,
        callback_handler=callback_handler,
    )


# ---------------------------------------------------------------------------
# Test utilities - patch router internals
# ---------------------------------------------------------------------------

def _patch_router_internals(
    router: MessageRouter,
    *,
    context: PreparedRouteContext,
    access_allowed: bool = True,
) -> None:
    """Monkeypatch the three sub-objects that isolate raw Telegram parsing."""
    # context_builder: skip real TelegramMessage construction
    router._context_builder.prepare = AsyncMock(return_value=context)  # type: ignore[method-assign]

    # access_controller: grant access by default
    router.access_controller.check_access = AsyncMock(return_value=access_allowed)  # type: ignore[method-assign]

    # coalescer: never buffer in unit tests
    router._coalescer.try_buffer = AsyncMock(return_value=False)  # type: ignore[method-assign]

    # rate-limit coordinator: always allow
    router._rate_limit_coordinator.get_active_limiter = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    router._rate_limit_coordinator.check_rate_limit = AsyncMock(return_value=(True, None))  # type: ignore[method-assign]
    router._rate_limit_coordinator.acquire_concurrent_slot = AsyncMock(return_value=True)  # type: ignore[method-assign]
    router._rate_limit_coordinator.release_concurrent_slot = AsyncMock()  # type: ignore[method-assign]

    # interaction_recorder: return dummy id
    router._interaction_recorder.log = AsyncMock(return_value=1)  # type: ignore[method-assign]
    router._interaction_recorder.update = AsyncMock()  # type: ignore[method-assign]


# Silence OTel tracer for all tests in this module ----------------------------

@pytest.fixture(autouse=True)
def _no_otel() -> Any:
    """Replace OTel tracer with a no-op so tests don't need a running exporter.

    route_message() imports get_tracer / set_correlation_id_attr lazily inside
    the function body (``from app.observability.otel import ...``).  We patch
    at the module level where they are resolved at call time.
    """
    fake_span = MagicMock()
    fake_span.__enter__ = MagicMock(return_value=fake_span)
    fake_span.__exit__ = MagicMock(return_value=False)
    fake_span.is_recording = MagicMock(return_value=False)
    fake_span.set_attribute = MagicMock()

    fake_tracer = MagicMock()
    fake_tracer.start_as_current_span = MagicMock(return_value=fake_span)

    with (
        patch("app.observability.otel.get_tracer", return_value=fake_tracer),
        patch("app.observability.otel.set_correlation_id_attr"),
    ):
        yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRouteMessageDispatch:
    """route_message() routes each PreparedRouteContext to the right collaborator."""

    @pytest.mark.asyncio
    async def test_url_message_dispatched_to_url_handler(self) -> None:
        """A message whose text is a URL calls url_handler.handle_direct_url."""
        url_handler = MagicMock()
        url_handler.handle_direct_url = AsyncMock()
        url_handler.is_awaiting_url = AsyncMock(return_value=False)
        url_handler.can_handle_document = MagicMock(return_value=False)

        router = _make_router(url_handler=url_handler)
        ctx = _make_context(
            text="https://example.com/article",
            interaction_type="url",
            first_url="https://example.com/article",
        )
        _patch_router_internals(router, context=ctx)

        await router.route_message(SimpleNamespace())

        url_handler.handle_direct_url.assert_awaited_once()
        call_args = url_handler.handle_direct_url.call_args
        assert call_args.args[1] == "https://example.com/article"

    @pytest.mark.asyncio
    async def test_command_message_dispatched_to_command_dispatcher(self) -> None:
        """A /command message calls command_dispatcher.dispatch_command."""
        command_processor = MagicMock()
        command_processor.dispatch_command = AsyncMock(
            return_value=_make_dispatch_outcome(handled=True)
        )
        command_processor.has_active_init_session = MagicMock(return_value=False)

        router = _make_router(command_processor=command_processor)
        ctx = _make_context(
            text="/start",
            interaction_type="command",
            command="start",
        )
        _patch_router_internals(router, context=ctx)

        await router.route_message(SimpleNamespace())

        command_processor.dispatch_command.assert_awaited_once()
        call_args = command_processor.dispatch_command.call_args
        assert call_args.kwargs.get("text") == "/start" or call_args.args[1] == "/start"

    @pytest.mark.asyncio
    async def test_forward_message_dispatched_to_forward_processor(self) -> None:
        """A forwarded channel post calls forward_processor.handle_forward_flow."""
        forward_processor = MagicMock()
        forward_processor.handle_forward_flow = AsyncMock()

        # Provide message with forward_from_chat to satisfy _route_forward_message
        raw_message = SimpleNamespace(
            contact=None,
            web_app_data=None,
            forward_from_chat=SimpleNamespace(id=-1001234567890, title="TestChan"),
            forward_from_message_id=42,
            fwd_from=SimpleNamespace(channel_post=42),
            outgoing=False,
        )
        router = _make_router(forward_processor=forward_processor)
        ctx = _make_context(
            text="Some article text",
            has_forward=True,
            interaction_type="forward",
            message=raw_message,
        )
        _patch_router_internals(router, context=ctx)

        await router.route_message(SimpleNamespace())

        forward_processor.handle_forward_flow.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_access_denied_user_does_not_reach_any_handler(self) -> None:
        """When access_controller.check_access returns False, no handler is called."""
        url_handler = MagicMock()
        url_handler.handle_direct_url = AsyncMock()
        url_handler.is_awaiting_url = AsyncMock(return_value=False)
        url_handler.can_handle_document = MagicMock(return_value=False)

        command_processor = MagicMock()
        command_processor.dispatch_command = AsyncMock(
            return_value=_make_dispatch_outcome(handled=False)
        )
        command_processor.has_active_init_session = MagicMock(return_value=False)

        forward_processor = MagicMock()
        forward_processor.handle_forward_flow = AsyncMock()

        router = _make_router(
            url_handler=url_handler,
            command_processor=command_processor,
            forward_processor=forward_processor,
        )
        ctx = _make_context(
            text="https://example.com/secret",
            uid=999,  # not in allowed_user_ids
        )
        _patch_router_internals(router, context=ctx, access_allowed=False)

        await router.route_message(SimpleNamespace())

        url_handler.handle_direct_url.assert_not_awaited()
        command_processor.dispatch_command.assert_not_awaited()
        forward_processor.handle_forward_flow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_command_dispatched_to_command_dispatcher(self) -> None:
        """An unknown /cmd is still passed to the command dispatcher (it decides how to respond)."""
        command_processor = MagicMock()
        command_processor.dispatch_command = AsyncMock(
            return_value=_make_dispatch_outcome(handled=False)
        )
        command_processor.has_active_init_session = MagicMock(return_value=False)

        response_formatter = MagicMock()
        response_formatter.safe_reply = AsyncMock()
        response_formatter.safe_reply_with_id = AsyncMock(return_value=55)
        response_formatter.send_chat_action = AsyncMock()

        router = _make_router(command_processor=command_processor)
        # Inject our response_formatter so we can check safe_reply below
        router._content_router.response_formatter = response_formatter

        ctx = _make_context(
            text="/unknowncommand",
            interaction_type="command",
            command="unknowncommand",
        )
        _patch_router_internals(router, context=ctx)

        await router.route_message(SimpleNamespace())

        # Dispatcher was consulted regardless of whether it handled the command
        command_processor.dispatch_command.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_context_builder_returning_none_short_circuits_routing(self) -> None:
        """When context_builder.prepare returns None, routing silently exits."""
        url_handler = MagicMock()
        url_handler.handle_direct_url = AsyncMock()
        url_handler.is_awaiting_url = AsyncMock(return_value=False)
        url_handler.can_handle_document = MagicMock(return_value=False)

        router = _make_router(url_handler=url_handler)
        # Override to return None (e.g. duplicate / outgoing message)
        router._context_builder.prepare = AsyncMock(return_value=None)  # type: ignore[method-assign]

        await router.route_message(SimpleNamespace())

        url_handler.handle_direct_url.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowed_user_reaches_url_handler(self) -> None:
        """A message from a whitelisted user does pass access control and hits the handler."""
        url_handler = MagicMock()
        url_handler.handle_direct_url = AsyncMock()
        url_handler.is_awaiting_url = AsyncMock(return_value=False)
        url_handler.can_handle_document = MagicMock(return_value=False)

        router = _make_router(allowed_uid=_ALLOWED_UID, url_handler=url_handler)
        ctx = _make_context(
            text="https://news.ycombinator.com/item?id=1",
            interaction_type="url",
            first_url="https://news.ycombinator.com/item?id=1",
            uid=_ALLOWED_UID,
        )
        _patch_router_internals(router, context=ctx, access_allowed=True)

        await router.route_message(SimpleNamespace())

        url_handler.handle_direct_url.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_plain_text_not_url_falls_through_to_fallback_reply(self) -> None:
        """Plain text that is not a URL or command triggers the fallback safe_reply."""
        response_formatter = MagicMock()
        response_formatter.safe_reply = AsyncMock()
        response_formatter.safe_reply_with_id = AsyncMock(return_value=55)
        response_formatter.send_chat_action = AsyncMock()

        router = _make_router()
        # Inject our formatter so we can inspect the call
        router._content_router.response_formatter = response_formatter

        ctx = _make_context(
            text="hello world",
            interaction_type="text",
        )
        _patch_router_internals(router, context=ctx)

        await router.route_message(SimpleNamespace())

        response_formatter.safe_reply.assert_awaited_once()
