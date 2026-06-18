"""Coverage for the bot's error reporting on command failures."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
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
        self.id = 999
        self.message_id = 999

    async def reply_text(self, text: str, **_kwargs: object) -> None:
        self._replies.append(text)


def _make_bot(database: Database) -> TelegramBot:
    cfg = make_test_app_config(db_path="/tmp/cmd-errors.db", allowed_user_ids=(1,))
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


async def test_error_during_summarize_reports_to_user(database: Database) -> None:
    bot = _make_bot(database)
    msg = FakeMessage("/summarize https://example.com")

    async def boom_url_flow(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("boom")

    bot.url_processor.handle_url_flow = boom_url_flow

    await bot._on_message(msg)
    assert any("error" in r.lower() for r in msg._replies)
