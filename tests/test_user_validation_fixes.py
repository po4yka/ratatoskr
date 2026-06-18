"""Unit tests for user validation fixes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from app.adapter_models.telegram.telegram_models import ChatType, TelegramMessage, TelegramUser
from app.adapters.telegram.telegram_bot import TelegramBot
from tests.conftest import make_test_app_config
from tests.telegram_bot_builders import AUDIT_REPOSITORY_BUILDER, RUNTIME_BUILDER

if TYPE_CHECKING:
    from app.db.session import Database


class FakeMessage:
    def __init__(self, text: str, uid: int, message_id: int = 101) -> None:
        class _User:
            def __init__(self, uid: int) -> None:
                self.id = uid

        class _Chat:
            id = 1

        self.text = text
        self.chat = _Chat()
        self.from_user = _User(uid)
        self._replies: list[str] = []
        self.id = message_id
        self.message_id = message_id

    async def reply_text(self, text: str, **_kwargs: object) -> None:
        self._replies.append(text)


def _make_bot(database: Database, allowed_ids: list[int]) -> TelegramBot:
    cfg = make_test_app_config(
        db_path="/tmp/user-validation.db", allowed_user_ids=tuple(allowed_ids)
    )
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


# ---------------------------------------------------------------------------
# Bot-level: integer/string user IDs and the access-check path
# ---------------------------------------------------------------------------


async def test_user_id_type_consistency(database: Database) -> None:
    """Integer user ID matches the allow-list."""
    bot = _make_bot(database, allowed_ids=[94225168])
    msg = FakeMessage("/help", uid=94225168)
    await bot._on_message(msg)
    assert any("commands" in reply.lower() for reply in msg._replies)


async def test_user_id_string_conversion(database: Database) -> None:
    """String user ID is normalised to int by the access pipeline."""
    bot = _make_bot(database, allowed_ids=[94225168])

    class StringUserMessage(FakeMessage):
        def __init__(self, text: str, uid_str: str) -> None:
            super().__init__(text, 0)
            self.from_user.id = uid_str  # type: ignore[assignment]

    msg = StringUserMessage("/help", "94225168")
    await bot._on_message(msg)
    assert any("commands" in reply.lower() for reply in msg._replies)


async def test_user_id_validation_with_different_types(database: Database) -> None:
    bot = _make_bot(database, allowed_ids=[94225168, 12345])
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    msg1 = FakeMessage("/help", uid=94225168)
    await bot._on_message(msg1)
    assert any("commands" in reply.lower() for reply in msg1._replies)

    msg2 = FakeMessage("/help", uid=12345)
    await bot._on_message(msg2)
    assert any("commands" in reply.lower() for reply in msg2._replies)

    msg3 = FakeMessage("/help", uid=99999)
    await bot._on_message(msg3)
    assert any("access denied" in reply.lower() for reply in msg3._replies)


async def test_empty_allowed_user_ids_raises(database: Database) -> None:
    """An empty allow-list should be rejected during bot construction."""
    with pytest.raises(RuntimeError):
        _make_bot(database, allowed_ids=[])


async def test_user_validation_logging(database: Database) -> None:
    """Bot logs the user id during message handling.

    Legacy version patched `app.adapters.telegram_bot.logger`; the module
    moved to `app.adapters.telegram.telegram_bot` during the SQLAlchemy
    port refactor, so the patch target was stale.
    """
    bot = _make_bot(database, allowed_ids=[94225168])

    with patch("app.adapters.telegram.telegram_bot.logger") as mock_logger:
        msg = FakeMessage("/help", uid=94225168)
        await bot._on_message(msg)

        mock_logger.info.assert_called()
        log_calls = [str(call) for call in mock_logger.info.call_args_list]
        assert any("94225168" in call for call in log_calls)


# ---------------------------------------------------------------------------
# Pure-Python: Telegram message and user adapter parsing
# ---------------------------------------------------------------------------


def test_telegram_message_parsing_with_enum_objects() -> None:
    class MockChatType:
        def __init__(self, name: str) -> None:
            self.name = name
            self.value = name.lower()

    class MockMessage:
        def __init__(self) -> None:
            self.id = 12345
            self.date = None
            self.text = "Test message"
            self.caption = None
            self.entities: list[Any] = []
            self.caption_entities: list[Any] = []
            self.photo = None
            self.video = None
            self.audio = None
            self.document = None
            self.sticker = None
            self.voice = None
            self.video_note = None
            self.animation = None
            self.contact = None
            self.location = None
            self.venue = None
            self.poll = None
            self.dice = None
            self.game = None
            self.invoice = None
            self.successful_payment = None
            self.story = None
            self.forward_from = None
            self.forward_from_chat = None
            self.forward_from_message_id = None
            self.forward_signature = None
            self.forward_sender_name = None
            self.forward_date = None
            self.reply_to_message = None
            self.edit_date = None
            self.media_group_id = None
            self.author_signature = None
            self.via_bot = None
            self.has_protected_content = None
            self.connected_website = None
            self.reply_markup = None
            self.views = None
            self.via_bot_user_id = None
            self.effect_id = None
            self.link_preview_options = None
            self.show_caption_above_media = None

    class MockUser:
        def __init__(self) -> None:
            self.id = 94225168
            self.is_bot = False
            self.first_name = "Test"
            self.last_name = "User"
            self.username = "testuser"
            self.language_code = "en"
            self.is_premium = None
            self.added_to_attachment_menu = None

    class MockChat:
        def __init__(self) -> None:
            self.id = 94225168
            self.type = MockChatType("PRIVATE")
            self.first_name = "Test"
            self.last_name = "User"
            self.title = None
            self.username = None
            self.is_forum = None
            self.photo = None
            self.active_usernames = None
            self.emoji_status_custom_emoji_id = None
            self.bio = None
            self.has_private_forwards = None
            self.has_restricted_voice_and_video_messages = None
            self.has_restricted_voice_and_video_messages_for_self = None
            self.description = None
            self.invite_link = None
            self.pinned_message = None
            self.permissions = None
            self.slow_mode_delay = None
            self.message_auto_delete_time = None
            self.has_aggressive_anti_spam_enabled = None
            self.has_hidden_members = None
            self.has_protected_content = None
            self.sticker_set_name = None
            self.can_set_sticker_set = None
            self.linked_chat_id = None
            self.location = None

    mock = MockMessage()
    mock.from_user = MockUser()  # type: ignore[attr-defined]
    mock.chat = MockChat()  # type: ignore[attr-defined]

    parsed = TelegramMessage.from_telegram_message(mock)

    assert parsed.message_id == 12345
    assert parsed.from_user is not None
    assert parsed.from_user.id == 94225168
    assert parsed.chat is not None
    assert parsed.chat.type == ChatType.PRIVATE


def test_telegram_user_id_conversion() -> None:
    user_int = TelegramUser.from_dict({"id": 94225168, "is_bot": False, "first_name": "Test"})
    assert user_int.id == 94225168
    assert isinstance(user_int.id, int)

    user_str = TelegramUser.from_dict({"id": "94225168", "is_bot": False, "first_name": "Test"})
    assert user_str.id == 94225168
    assert isinstance(user_str.id, int)

    user_invalid = TelegramUser.from_dict({"id": "invalid", "is_bot": False, "first_name": "Test"})
    assert user_invalid.id == 0  # fallback

    user_none = TelegramUser.from_dict({"id": None, "is_bot": False, "first_name": "Test"})
    assert user_none.id == 0
