"""Telethon runtime compatibility helpers for the existing bot surface.

This module is a thin facade. All implementation has been moved to cohesive
submodules under ``app/adapters/telegram/compat_*.py``. Every public name is
re-exported here so existing ``from app.adapters.telethon_compat import X``
calls continue to work unchanged.
"""

from __future__ import annotations

# -- stdlib re-exports kept for importers that reach through this facade ------
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

# -- Submodule re-exports -----------------------------------------------------
from app.adapters.telegram.compat_adapters import (
    TelethonCallbackQueryAdapter,
    TelethonMessageAdapter,
    TelethonReactionAdapter,
)
from app.adapters.telegram.compat_clients import (
    TelethonBotClient,
    TelethonUserClient,
)
from app.adapters.telegram.compat_entities import (
    _TELETHON_ENTITY_TYPES,
    _build_typing_tl_action,
    _peer_to_id,
    _translate_entities,
)
from app.adapters.telegram.compat_keyboards import (
    _filter_send_kwargs,
    _inline_button_to_telethon,
    _reply_button_to_telethon,
    to_telethon_buttons,
)
from app.adapters.telegram.compat_telethon import (
    TELETHON_AVAILABLE,
    Button,
    SessionPasswordNeededError,
    TelegramClient,
    _SessionPasswordNeededError,
    events,
    functions,
    types,
    utils,
)
from app.adapters.telegram.compat_types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ParseMode,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
    _Entity,
    _Object,
    normalize_parse_mode,
)
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger

logger = get_logger(__name__)

__all__ = [
    "TELETHON_AVAILABLE",
    "_TELETHON_ENTITY_TYPES",
    "Any",
    "BotCommand",
    "BotCommandScopeAllPrivateChats",
    "Button",
    "InlineKeyboardButton",
    "InlineKeyboardMarkup",
    "KeyboardButton",
    "ParseMode",
    "ReplyKeyboardMarkup",
    "ReplyKeyboardRemove",
    "SessionPasswordNeededError",
    "StrEnum",
    "TelegramClient",
    "TelethonBotClient",
    "TelethonCallbackQueryAdapter",
    "TelethonMessageAdapter",
    "TelethonReactionAdapter",
    "TelethonUserClient",
    "WebAppInfo",
    "_Entity",
    "_Object",
    "_SessionPasswordNeededError",
    "_build_typing_tl_action",
    "_filter_send_kwargs",
    "_inline_button_to_telethon",
    "_peer_to_id",
    "_reply_button_to_telethon",
    "_translate_entities",
    "annotations",
    "cast",
    "dataclass",
    "events",
    "functions",
    "get_logger",
    "logger",
    "normalize_parse_mode",
    "raise_if_cancelled",
    "to_telethon_buttons",
    "types",
    "utils",
]
