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
        actor=types.PeerUser(user_id=777),
        new_reactions=[types.ReactionEmoji(emoticon="👍")],
    )
    adapter = TelethonReactionAdapter(update)
    assert adapter.message_id == 42
    assert adapter.emoji == "👍"
    assert adapter.chat_id == 555
    # ReactionFeedbackHandler compares actor_id against the owner, so the
    # adapter must surface it -- and must yield None (fail closed) when the
    # update carries no actor.
    assert adapter.actor_id == 777
    assert TelethonReactionAdapter(SimpleNamespace(peer=None, msg_id=1)).actor_id is None


def _web_view_service_update(data: str = '{"mode":"otp","value":"12345"}'):
    """A real Mini App sendData() update: a MessageService, not a Message."""
    import datetime

    from telethon.tl import types

    return types.UpdateNewMessage(
        message=types.MessageService(
            id=1234,
            peer_id=types.PeerUser(user_id=555),
            date=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            action=types.MessageActionWebViewDataSentMe(text="Send", data=data),
            from_id=types.PeerUser(user_id=555),
        ),
        pts=1,
        pts_count=1,
    )


def test_message_adapter_exposes_web_app_data_from_a_service_message() -> None:
    from app.adapters.telethon_compat import TelethonMessageAdapter

    adapter = TelethonMessageAdapter(_web_view_service_update(), bot=None)

    assert adapter.web_app_data is not None
    assert adapter.web_app_data.data == '{"mode":"otp","value":"12345"}'
    assert adapter.web_app_data.button_text == "Send"
    # init_session_handler.handle_web_app_data needs both of these.
    assert adapter.from_user.id == 555
    assert adapter.id == 1234


def test_message_adapter_web_app_data_is_none_for_an_ordinary_message() -> None:
    from types import SimpleNamespace

    from app.adapters.telethon_compat import TelethonMessageAdapter

    event = SimpleNamespace(message=SimpleNamespace(id=1, action=None), sender=None)
    assert TelethonMessageAdapter(event, bot=None).web_app_data is None


async def test_add_message_handler_also_subscribes_to_web_app_service_messages() -> None:
    """events.NewMessage drops MessageService, so sendData() needs a raw handler.

    Without the second subscription the /init_session OTP and 2FA steps never
    fire: the session sits at waiting_otp until its 300 s TTL expires, and the
    flow has no text fallback.
    """
    from telethon.tl import types

    from app.adapters.telethon_compat import TelethonBotClient

    registered: list[object] = []

    class _FakeTelethonClient:
        def on(self, event_spec: object):
            def _register(fn):
                registered.append((event_spec, fn))
                return fn

            return _register

    client = object.__new__(TelethonBotClient)
    client._client = _FakeTelethonClient()

    seen: list[object] = []

    async def _handler(message: object) -> None:
        seen.append(message)

    client.add_message_handler(_handler)

    assert len(registered) == 2, "expected a NewMessage and a raw-update subscription"
    raw_callback = registered[1][1]

    await raw_callback(_web_view_service_update())
    assert len(seen) == 1
    assert seen[0].web_app_data.data == '{"mode":"otp","value":"12345"}'

    # An ordinary message on the raw stream must not be double-delivered: the
    # NewMessage subscription already owns it.
    await raw_callback(
        types.UpdateNewMessage(
            message=types.Message(id=2, peer_id=types.PeerUser(user_id=555), message="hi"),
            pts=2,
            pts_count=1,
        )
    )
    assert len(seen) == 1


def test_filter_send_kwargs_translates_disable_web_page_preview() -> None:
    from app.adapters.telethon_compat import _filter_send_kwargs

    # disable_web_page_preview is honored on the first-send path now, not dropped.
    assert _filter_send_kwargs({"disable_web_page_preview": True})["link_preview"] is False
    assert _filter_send_kwargs({"disable_web_page_preview": False})["link_preview"] is True
    # An explicit link_preview wins; unknown kwargs are still dropped.
    out = _filter_send_kwargs({"disable_web_page_preview": True, "link_preview": True, "x": 1})
    assert out == {"link_preview": True}
    assert _filter_send_kwargs({}) == {}
