"""Button-translation behavior in the Telethon compat layer."""

from __future__ import annotations

import pytest

pytest.importorskip("telethon")

from telethon import types

from app.adapters.telethon_compat import (
    InlineKeyboardButton,
    KeyboardButton,
    WebAppInfo,
    _inline_button_to_telethon,
    _reply_button_to_telethon,
)


def test_inline_web_app_button_is_real_webview() -> None:
    btn = _inline_button_to_telethon(
        InlineKeyboardButton("Open", web_app=WebAppInfo(url="https://app.example.com"))
    )
    assert isinstance(btn, types.KeyboardButtonWebView)
    assert btn.url == "https://app.example.com"


def test_reply_web_app_button_is_simple_webview() -> None:
    # SimpleWebView is the only variant from which WebApp.sendData() works.
    btn = _reply_button_to_telethon(
        KeyboardButton("Connect", web_app=WebAppInfo(url="https://app.example.com/init"))
    )
    assert isinstance(btn, types.KeyboardButtonSimpleWebView)
    assert btn.url == "https://app.example.com/init"


def test_plain_url_and_callback_buttons_unchanged() -> None:
    url_btn = _inline_button_to_telethon(InlineKeyboardButton("Site", url="https://x.com"))
    assert getattr(url_btn, "url", None) == "https://x.com"
    cb_btn = _inline_button_to_telethon(InlineKeyboardButton("Save", callback_data="save:1"))
    assert getattr(cb_btn, "data", None) == b"save:1"
