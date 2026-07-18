from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.adapters.telegram.message_handler import MessageHandler
from tests.conftest import make_test_app_config


class _ResponseFormatterStub:
    def __init__(self) -> None:
        self._lang = "en"
        self.safe_reply = AsyncMock()
        self.send_error_notification = AsyncMock()
        self.sender = SimpleNamespace(safe_reply=AsyncMock())
        self.notifications = SimpleNamespace(send_error_notification=AsyncMock())
        self.database = SimpleNamespace(send_topic_search_results=AsyncMock())
        self.summaries = SimpleNamespace(send_russian_translation=AsyncMock())


def test_message_handler_wires_callback_handler_during_construction(tmp_path) -> None:
    callback_handler = SimpleNamespace(handle_callback=AsyncMock())
    message_router = SimpleNamespace(
        callback_handler=callback_handler,
        route_message=AsyncMock(),
    )

    handler = MessageHandler(
        cfg=make_test_app_config(db_path=":memory:"),
        db=None,
        audit_repo=None,
        task_manager=None,
        access_controller=cast("Any", SimpleNamespace()),
        url_handler=cast("Any", SimpleNamespace(url_processor=SimpleNamespace())),
        command_dispatcher=cast("Any", SimpleNamespace()),
        callback_handler=cast("Any", callback_handler),
        message_router=cast("Any", message_router),
    )

    assert handler.message_router.callback_handler is handler.callback_handler


def _make_handler_with_access(*, access_granted: bool) -> tuple[MessageHandler, Any, Any]:
    callback_handler = SimpleNamespace(handle_callback=AsyncMock(return_value=True))
    access_controller = SimpleNamespace(check_access=AsyncMock(return_value=access_granted))
    handler = MessageHandler(
        cfg=make_test_app_config(db_path=":memory:"),
        db=None,
        audit_repo=None,
        task_manager=None,
        access_controller=cast("Any", access_controller),
        url_handler=cast("Any", SimpleNamespace(url_processor=SimpleNamespace())),
        command_dispatcher=cast("Any", SimpleNamespace()),
        callback_handler=cast("Any", callback_handler),
        message_router=cast(
            "Any",
            SimpleNamespace(callback_handler=callback_handler, route_message=AsyncMock()),
        ),
    )
    return handler, access_controller, callback_handler


def _make_callback_query(uid: int, data: str = "cb:export:1") -> Any:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=uid),
        message=SimpleNamespace(),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_callback_query_denied_for_unauthorized_user() -> None:
    handler, access_controller, callback_handler = _make_handler_with_access(access_granted=False)
    callback_query = _make_callback_query(uid=999)

    await handler.handle_callback_query(callback_query)

    access_controller.check_access.assert_awaited_once()
    # The privileged action must never run for a denied user...
    callback_handler.handle_callback.assert_not_called()
    # ...and the tap is answered with a denial alert.
    callback_query.answer.assert_awaited_once()
    assert callback_query.answer.call_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_callback_query_allowed_for_authorized_user() -> None:
    handler, access_controller, callback_handler = _make_handler_with_access(access_granted=True)
    callback_query = _make_callback_query(uid=42)

    await handler.handle_callback_query(callback_query)

    access_controller.check_access.assert_awaited_once()
    callback_handler.handle_callback.assert_awaited_once_with(callback_query, 42, "cb:export:1")
