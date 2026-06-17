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


def test_copy_text_button_is_keyboard_button_copy() -> None:
    btn = _inline_button_to_telethon(
        InlineKeyboardButton("📋", copy_text="https://src.example.com/article")
    )
    assert isinstance(btn, types.KeyboardButtonCopy)
    assert btn.copy_text == "https://src.example.com/article"


def test_summary_keyboard_adds_copy_button_only_with_source_url() -> None:
    from app.adapters.external.formatting.summary.action_buttons import create_inline_keyboard

    with_url = create_inline_keyboard(7, source_url="https://src.example.com/a")
    copy_btns = [
        b
        for row in with_url.inline_keyboard
        for b in row
        if getattr(b, "copy_text", None) == "https://src.example.com/a"
    ]
    assert len(copy_btns) == 1

    without_url = create_inline_keyboard(7)
    assert not any(
        getattr(b, "copy_text", None) for row in without_url.inline_keyboard for b in row
    )


def test_oversized_url_skips_copy_button_but_keeps_keyboard() -> None:
    from app.adapters.external.formatting.summary.action_buttons import create_inline_keyboard

    long_url = "https://example.com/" + "a" * 300  # > 256 UTF-8 bytes
    kb = create_inline_keyboard(7, source_url=long_url)
    assert kb is not None  # the rest of the keyboard still builds
    assert not any(getattr(b, "copy_text", None) for row in kb.inline_keyboard for b in row)
    # the standard rows survive
    assert any(getattr(b, "callback_data", None) for row in kb.inline_keyboard for b in row)
