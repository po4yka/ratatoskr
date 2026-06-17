"""TelethonBotClient raw-MTProto helper behavior (menu button, reactions)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("telethon")

from telethon import functions

from app.adapters.telethon_compat import TelethonBotClient


def _client_with_mock() -> tuple[TelethonBotClient, AsyncMock]:
    client = object.__new__(TelethonBotClient)
    mock = AsyncMock()
    client._client = mock
    return client, mock


async def test_set_chat_menu_button_builds_webapp_request() -> None:
    client, mock = _client_with_mock()
    await client.set_chat_menu_button(text="Open", url="https://app.example.com")
    assert mock.await_count == 1
    req = mock.await_args.args[0]
    assert isinstance(req, functions.bots.SetBotMenuButtonRequest)
    assert req.button.url == "https://app.example.com"
    assert req.button.text == "Open"


async def test_set_chat_menu_button_noop_without_url() -> None:
    client, mock = _client_with_mock()
    await client.set_chat_menu_button(url=None)
    mock.assert_not_awaited()


async def test_react_sends_single_emoji_reaction() -> None:
    from telethon import functions

    client, mock = _client_with_mock()
    await client.react(chat_id=123, message_id=45, emoji="✅")
    assert mock.get_input_entity.await_args.args == (123,)
    req = mock.call_args.args[0]
    assert isinstance(req, functions.messages.SendReactionRequest)
    assert req.msg_id == 45
    assert len(req.reaction) == 1 and req.reaction[0].emoticon == "✅"


def test_filter_send_kwargs_translates_disable_web_page_preview() -> None:
    from app.adapters.telethon_compat import _filter_send_kwargs

    # disable_web_page_preview is honored on the first-send path now, not dropped.
    assert _filter_send_kwargs({"disable_web_page_preview": True})["link_preview"] is False
    assert _filter_send_kwargs({"disable_web_page_preview": False})["link_preview"] is True
    # An explicit link_preview wins; unknown kwargs are still dropped.
    out = _filter_send_kwargs({"disable_web_page_preview": True, "link_preview": True, "x": 1})
    assert out == {"link_preview": True}
    assert _filter_send_kwargs({}) == {}
