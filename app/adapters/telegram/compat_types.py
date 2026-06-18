"""Keyboard/markup data classes and parse-mode helpers.

These are pure Python dataclasses that mirror the aiogram/Bot API type surface;
they carry no Telethon dependency so they are always importable even in the
minimal CI environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ParseMode(StrEnum):
    HTML = "html"
    MARKDOWN = "markdown"
    DISABLED = "disabled"


@dataclass(slots=True, frozen=True)
class WebAppInfo:
    url: str


@dataclass(slots=True, frozen=True)
class KeyboardButton:
    text: str
    request_contact: bool = False
    web_app: WebAppInfo | None = None


@dataclass(slots=True, frozen=True)
class ReplyKeyboardMarkup:
    keyboard: list[list[KeyboardButton]]
    resize_keyboard: bool = True
    one_time_keyboard: bool = True


@dataclass(slots=True, frozen=True)
class ReplyKeyboardRemove:
    remove_keyboard: bool = True


@dataclass(slots=True, frozen=True)
class InlineKeyboardButton:
    text: str
    callback_data: str | bytes | None = None
    url: str | None = None
    web_app: WebAppInfo | None = None
    copy_text: str | None = None
    style: str | None = None


@dataclass(slots=True, frozen=True)
class InlineKeyboardMarkup:
    inline_keyboard: list[list[InlineKeyboardButton]]


@dataclass(slots=True, frozen=True)
class BotCommand:
    command: str
    description: str


@dataclass(slots=True, frozen=True)
class BotCommandScopeAllPrivateChats:
    """Compatibility marker for the existing command setup code."""


@dataclass(slots=True)
class _Object:
    id: object = None
    first_name: str | None = None
    username: str | None = None
    is_bot: bool = False
    title: str | None = None
    type: str | None = None


@dataclass(slots=True)
class _Entity:
    """aiogram-shaped message entity translated from a raw Telethon entity."""

    type: str
    offset: int = 0
    length: int = 0
    url: str | None = None


def normalize_parse_mode(mode: str | ParseMode | None) -> str | None:
    if mode is None:
        return None
    raw = str(mode.value if isinstance(mode, ParseMode) else mode).lower()
    if raw in {"html", "parsemode.html"}:
        return "html"
    if raw in {"markdown", "md", "parsemode.markdown"}:
        return "markdown"
    if raw in {"disabled", "none", "parsemode.disabled"}:
        return None
    return str(mode)


__all__ = [
    "BotCommand",
    "BotCommandScopeAllPrivateChats",
    "InlineKeyboardButton",
    "InlineKeyboardMarkup",
    "KeyboardButton",
    "ParseMode",
    "ReplyKeyboardMarkup",
    "ReplyKeyboardRemove",
    "WebAppInfo",
    "_Entity",
    "_Object",
    "normalize_parse_mode",
]
