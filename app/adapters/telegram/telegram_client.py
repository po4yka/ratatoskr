from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from app.adapters.telegram.telethon_compat import (
    TELETHON_AVAILABLE,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    TelethonBotClient,
)
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.config import AppConfig

logger = get_logger(__name__)

# Commands that expose owner-only administration or debugging surface. These are
# advertised only in the owners' own private chats (per-peer command scope), never
# via the default / all-private-chats scopes, so a non-owner who opens a chat with
# the bot never sees them enumerated in the command menu. The command handlers
# still enforce access control independently; this only governs advertisement.
ADMIN_COMMAND_NAMES = frozenset({"admin", "dbinfo", "dbverify", "models", "setmodel", "clearcache"})


class TelegramClient:
    """Handles Telethon bot client setup and operations."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.client: TelethonBotClient | None = None
        self.topic_manager: Any = None

        if not TELETHON_AVAILABLE:
            self.client = None
        else:
            self.client = TelethonBotClient(
                name="ratatoskr_bot",
                api_id=self.cfg.telegram.api_id,
                api_hash=self.cfg.telegram.api_hash,
                bot_token=self.cfg.telegram.bot_token,
                session_dir="/data",
            )

    async def start(
        self,
        message_handler: Callable[[Any], Awaitable[None]],
        callback_query_handler: Callable[[Any], Awaitable[None]] | None = None,
        reaction_handler: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        """Start the Telegram client with message, callback and reaction handlers."""
        if not self.client:
            logger.warning("telegram_client_not_available")
            return

        await self.client.start()
        handler_count = 0

        self.client.add_message_handler(message_handler)
        handler_count += 1

        if callback_query_handler:
            self.client.add_callback_query_handler(callback_query_handler)
            handler_count += 1

        if reaction_handler and hasattr(self.client, "add_reaction_handler"):
            self.client.add_reaction_handler(reaction_handler)
            handler_count += 1

        logger.info(
            "handlers_registered",
            extra={
                "handler_count": handler_count,
                "has_callback": callback_query_handler is not None,
            },
        )
        await self._setup_bot_commands()
        await self._setup_forum_topics()
        await idle()

    async def _setup_bot_commands(self) -> None:
        """Set up bot commands for different languages."""
        if not self.client:
            return
        commands_en = [
            BotCommand("summarize", "Summarize a URL"),
            BotCommand("search", "Search your summaries"),
            BotCommand("unread", "Show unread articles"),
            BotCommand("read", "Mark article as read"),
            BotCommand("summarize_all", "Summarize multiple URLs"),
            BotCommand("cancel", "Cancel pending operation"),
            BotCommand("help", "Show help and usage"),
            BotCommand("start", "Welcome and instructions"),
            BotCommand("admin", "Admin overview / jobs / errors"),
            BotCommand("dbinfo", "Show database stats"),
            BotCommand("dbverify", "Verify database integrity"),
            BotCommand("models", "Show active model config"),
            BotCommand("setmodel", "Change a model at runtime"),
            BotCommand("clearcache", "Clear internal cache"),
            BotCommand("listen", "Generate audio from summary"),
            BotCommand("digest", "Generate channel digest"),
            BotCommand("channels", "List subscribed channels"),
            BotCommand("subscribe", "Subscribe to a channel"),
            BotCommand("unsubscribe", "Unsubscribe from a channel"),
        ]
        commands_ru = [
            BotCommand("summarize", "Суммировать ссылку"),
            BotCommand("search", "Поиск по резюме"),
            BotCommand("unread", "Непрочитанные статьи"),
            BotCommand("read", "Отметить прочитанным"),
            BotCommand("summarize_all", "Суммировать несколько"),
            BotCommand("cancel", "Отменить операцию"),
            BotCommand("help", "Помощь и инструкция"),
            BotCommand("start", "Приветствие"),
            BotCommand("admin", "Обзор / задачи / ошибки"),
            BotCommand("dbinfo", "Статистика БД"),
            BotCommand("dbverify", "Проверка БД"),
            BotCommand("models", "Конфигурация моделей"),
            BotCommand("setmodel", "Сменить модель"),
            BotCommand("clearcache", "Очистить кэш"),
            BotCommand("listen", "Озвучить резюме"),
            BotCommand("digest", "Дайджест каналов"),
            BotCommand("channels", "Список подписок"),
            BotCommand("subscribe", "Подписаться на канал"),
            BotCommand("unsubscribe", "Отписаться от канала"),
        ]
        public_en = [c for c in commands_en if c.command not in ADMIN_COMMAND_NAMES]
        public_ru = [c for c in commands_ru if c.command not in ADMIN_COMMAND_NAMES]
        owner_ids = self.cfg.telegram.allowed_user_ids
        try:
            # Public commands: advertised to every user (default + all private chats).
            await self.client.set_bot_commands(public_en)
            await self.client.set_bot_commands(public_en, scope=BotCommandScopeAllPrivateChats())
            await self.client.set_bot_commands(public_ru, language_code="ru")
            await self.client.set_bot_commands(
                public_ru,
                scope=BotCommandScopeAllPrivateChats(),
                language_code="ru",
            )
            # Full set (public + admin/debug) advertised only in each owner's chat.
            await self._advertise_owner_commands(commands_en, commands_ru, owner_ids)
            with contextlib.suppress(Exception):
                await self.client.set_bot_description(
                    "Ratatoskr: Summarize URLs, YouTube videos, and forwarded posts. "
                    "Get structured summaries with key ideas, entities, and tags.",
                    language_code="en",
                )
                await self.client.set_bot_short_description(
                    "Summarize articles & videos into bite-sized insights",
                    language_code="en",
                )
                await self.client.set_bot_description(
                    "Ratatoskr: Резюме ссылок, YouTube видео и пересланных постов. "
                    "Структурированные саммари с ключевыми идеями, сущностями и тегами.",
                    language_code="ru",
                )
                await self.client.set_bot_short_description(
                    "Резюме статей и видео в краткие инсайты",
                    language_code="ru",
                )
            with contextlib.suppress(Exception):
                api_base = (self.cfg.telegram.api_base_url or "").rstrip("/")
                if api_base:
                    await self.client.set_chat_menu_button(text="Open", url=api_base)
            logger.info(
                "bot_commands_set",
                extra={
                    "count_public": len(public_en),
                    "count_owner": len(commands_en),
                    "owner_count": len(owner_ids),
                },
            )
        except Exception as exc:
            logger.warning("bot_commands_set_failed", extra={"error": str(exc)})

    async def _advertise_owner_commands(
        self,
        commands_en: list[BotCommand],
        commands_ru: list[BotCommand],
        owner_ids: tuple[int, ...],
    ) -> None:
        """Advertise the full command set in each owner's own private chat.

        Uses the per-peer command scope so admin/debug commands appear in the
        owner's autocomplete menu without being enumerated to every user.
        Resolving an owner's input peer can fail if the bot has not seen that
        user since the last restart (they have not messaged it yet); that owner
        simply keeps the public menu until they do. A per-owner failure must not
        abort setup for the others, so each owner is isolated.
        """
        if not self.client:
            return
        for uid in owner_ids:
            try:
                await self.client.set_bot_commands(commands_en, peer=uid)
                await self.client.set_bot_commands(commands_ru, peer=uid, language_code="ru")
            except Exception as exc:
                logger.warning(
                    "owner_bot_commands_set_failed",
                    extra={"user_id": uid, "error": str(exc)},
                )

    async def _setup_forum_topics(self) -> None:
        """Initialize forum topics for allowed users' private chats."""
        if self.topic_manager is None or not self.client:
            return
        if not self.cfg.telegram.allowed_user_ids:
            return

        for uid in self.cfg.telegram.allowed_user_ids:
            try:
                await self.topic_manager.ensure_default_topics(self.client, uid)
            except Exception as exc:
                logger.warning(
                    "forum_topics_setup_failed",
                    extra={"user_id": uid, "error": str(exc)},
                )


async def idle() -> None:
    """Simple idle loop to keep the client running."""
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:  # pragma: no cover
        return
