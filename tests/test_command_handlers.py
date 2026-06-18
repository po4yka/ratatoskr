"""Smoke tests for command handlers not covered by test_commands.py.

Covers OnboardingHandler (/start, /help). AdminHandler and
URLCommandsHandler are exercised via the bot integration paths in
test_commands.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from app.adapters.telegram.telegram_bot import TelegramBot
from tests.conftest import make_test_app_config
from tests.telegram_bot_builders import AUDIT_REPOSITORY_BUILDER, RUNTIME_BUILDER

if TYPE_CHECKING:
    from app.db.session import Database


class FakeMessage:
    def __init__(self, text: str, uid: int = 1) -> None:
        class _User:
            def __init__(self, uid: int) -> None:
                self.id = uid

        class _Chat:
            id = 1

        self.text = text
        self.chat = _Chat()
        self.from_user = _User(uid)
        self._replies: list[str] = []
        self.id = 200
        self.message_id = 200

    async def reply_text(self, text: str, **_kwargs: object) -> None:
        self._replies.append(text)


def _make_bot(database: Database) -> TelegramBot:
    cfg = make_test_app_config(db_path="/tmp/cmd-handlers.db", allowed_user_ids=(1,))
    from app.adapters import telegram_bot as tbmod

    tbmod.Client = object
    tbmod.filters = None

    with patch("app.adapters.openrouter.openrouter_client.OpenRouterClient") as mock_or:
        mock_or.return_value = AsyncMock()
        return TelegramBot(
            cfg=cfg,
            db=database,
            runtime_builder=RUNTIME_BUILDER,
            audit_repository_builder=AUDIT_REPOSITORY_BUILDER,
        )


async def test_start_command_replies(database: Database) -> None:
    bot = _make_bot(database)
    msg = FakeMessage("/start")
    await bot._on_message(msg)
    await bot._shutdown()
    assert msg._replies, "/start should produce at least one reply"


async def test_help_command_lists_commands(database: Database) -> None:
    bot = _make_bot(database)
    msg = FakeMessage("/help")
    await bot._on_message(msg)
    await bot._shutdown()
    assert any("Commands" in r for r in msg._replies)
