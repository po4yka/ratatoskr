"""Keyboard/button converters: local dataclasses → Telethon button structures."""

from __future__ import annotations

from typing import Any

from app.adapters.telegram.compat_telethon import Button, types
from app.adapters.telegram.compat_types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)


def to_telethon_buttons(reply_markup: Any) -> Any:
    """Translate local keyboard dataclasses to Telethon button structures."""
    if reply_markup is None or Button is None:
        return None
    if isinstance(reply_markup, InlineKeyboardMarkup):
        rows = []
        for row in reply_markup.inline_keyboard:
            converted = [
                converted_button
                for button in row
                if (converted_button := _inline_button_to_telethon(button)) is not None
            ]
            rows.append(converted)
        return rows
    if isinstance(reply_markup, ReplyKeyboardMarkup):
        return [
            [_reply_button_to_telethon(button) for button in row] for row in reply_markup.keyboard
        ]
    if isinstance(reply_markup, ReplyKeyboardRemove):
        return Button.clear()
    return reply_markup


def _inline_button_to_telethon(button: InlineKeyboardButton) -> Any:
    if button.url:
        return Button.url(button.text, button.url)
    if button.web_app:
        # Real in-Telegram Mini App button (in-app container + initData +
        # postEvent channel), not an external browser tab. Telethon's Button
        # helper has no webview constructor, so emit the raw TL type --
        # build_reply_markup accepts it (KeyboardButton family). Fall back to a
        # URL button only when telethon types are unavailable (minimal env).
        if types is not None:
            return types.KeyboardButtonWebView(text=button.text, url=button.web_app.url)
        return Button.url(button.text, button.web_app.url)
    if button.copy_text and types is not None:
        # One-tap copy-to-clipboard button (no Button helper exists for it).
        return types.KeyboardButtonCopy(text=button.text, copy_text=button.copy_text)
    data = button.callback_data or ""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return Button.inline(button.text, data=data)


def _reply_button_to_telethon(button: KeyboardButton) -> Any:
    if button.request_contact:
        return Button.request_phone(button.text)
    if button.web_app:
        # SimpleWebView (reply-keyboard variant): required for WebApp.sendData()
        # to reach the bot -- inline webview buttons cannot send data back.
        if types is not None:
            return types.KeyboardButtonSimpleWebView(text=button.text, url=button.web_app.url)
        return Button.url(button.text, button.web_app.url)
    return Button.text(button.text)


def _filter_send_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    allowed = {"link_preview", "file", "silent"}
    filtered = {key: value for key, value in kwargs.items() if key in allowed}
    # Translate aiogram-style disable_web_page_preview -> Telethon link_preview.
    # This is the shared chokepoint for first-send AND reply; without it the
    # kwarg was silently dropped and link-preview suppression worked only on
    # edits. An explicit link_preview wins. (The edit path translates its own.)
    if "disable_web_page_preview" in kwargs and "link_preview" not in filtered:
        filtered["link_preview"] = not bool(kwargs["disable_web_page_preview"])
    return filtered


__all__ = [
    "_filter_send_kwargs",
    "_inline_button_to_telethon",
    "_reply_button_to_telethon",
    "to_telethon_buttons",
]
