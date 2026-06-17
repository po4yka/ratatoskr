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


async def test_send_cover_message_inverts_media_and_carries_url() -> None:
    from telethon import functions

    client, mock = _client_with_mock()
    await client.send_cover_message(
        chat_id=99, text="<b>Title</b>", url="https://src.example.com/a"
    )
    req = mock.call_args.args[0]
    assert isinstance(req, functions.messages.SendMessageRequest)
    assert req.invert_media is True  # preview floated above the text
    assert "https://src.example.com/a" in req.message  # URL present -> preview generated
    assert "Title" in req.message


async def test_send_cover_message_noop_without_url() -> None:
    client, mock = _client_with_mock()
    assert await client.send_cover_message(chat_id=99, text="x", url="") is None
    mock.assert_not_awaited()


def test_reaction_adapter_extracts_fields() -> None:
    from types import SimpleNamespace

    from telethon import types

    from app.adapters.telethon_compat import TelethonReactionAdapter

    update = SimpleNamespace(
        peer=types.PeerUser(user_id=555),
        msg_id=42,
        new_reactions=[types.ReactionEmoji(emoticon="👍")],
    )
    adapter = TelethonReactionAdapter(update)
    assert adapter.message_id == 42
    assert adapter.emoji == "👍"
    assert adapter.chat_id == 555


def test_filter_send_kwargs_translates_disable_web_page_preview() -> None:
    from app.adapters.telethon_compat import _filter_send_kwargs

    # disable_web_page_preview is honored on the first-send path now, not dropped.
    assert _filter_send_kwargs({"disable_web_page_preview": True})["link_preview"] is False
    assert _filter_send_kwargs({"disable_web_page_preview": False})["link_preview"] is True
    # An explicit link_preview wins; unknown kwargs are still dropped.
    out = _filter_send_kwargs({"disable_web_page_preview": True, "link_preview": True, "x": 1})
    assert out == {"link_preview": True}
    assert _filter_send_kwargs({}) == {}
