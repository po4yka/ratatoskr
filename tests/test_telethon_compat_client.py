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
