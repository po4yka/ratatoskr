"""Unit tests for Telegram message models and validation."""

import unittest
from datetime import datetime

from app.adapter_models.telegram.telegram_models import (
    ChatType,
    MediaType,
    MessageEntity,
    MessageEntityType,
    TelegramChat,
    TelegramMessage,
    TelegramUser,
)


class TestTelegramUser(unittest.TestCase):
    """Test TelegramUser model."""

    def test_from_dict_minimal(self):
        """Test creating TelegramUser from minimal dictionary; optional fields default to None."""
        data = {
            "id": 12345,
            "is_bot": False,
            "first_name": "John",
        }
        user = TelegramUser.from_dict(data)

        assert user.id == 12345
        assert not user.is_bot
        assert user.first_name == "John"
        assert user.last_name is None
        assert user.username is None
        assert user.language_code is None


class TestTelegramChat(unittest.TestCase):
    """Test TelegramChat model."""

    def test_from_dict_channel(self):
        """Test creating channel chat from dictionary."""
        data = {
            "id": -10012345,
            "type": "channel",
            "title": "Test Channel",
            "username": "testchannel",
        }
        chat = TelegramChat.from_dict(data)

        assert chat.id == -10012345
        assert chat.type == ChatType.CHANNEL
        assert chat.title == "Test Channel"
        assert chat.username == "testchannel"


class TestMessageEntity(unittest.TestCase):
    """Test MessageEntity model."""

    def test_from_dict_text_link(self):
        """Test creating text link entity from dictionary."""
        data = {
            "type": "text_link",
            "offset": 10,
            "length": 20,
            "url": "https://example.com",
        }
        entity = MessageEntity.from_dict(data)

        assert entity.type == MessageEntityType.TEXT_LINK
        assert entity.offset == 10
        assert entity.length == 20
        assert entity.url == "https://example.com"

    def test_from_dict_mention(self):
        """Test creating mention entity — verifies nested user object parsing."""
        user_data = {
            "id": 12345,
            "is_bot": False,
            "first_name": "John",
        }
        data = {
            "type": "text_mention",
            "offset": 0,
            "length": 5,
            "user": user_data,
        }
        entity = MessageEntity.from_dict(data)

        assert entity.type == MessageEntityType.TEXT_MENTION
        assert entity.offset == 0
        assert entity.length == 5
        assert entity.user is not None
        assert entity.user.id == 12345


class TestTelegramMessage(unittest.TestCase):
    """Test TelegramMessage model."""

    class SimpleMockMessage:
        """Simple mock message class for testing (Mock objects don't work well with Pydantic)."""

        def __init__(self):
            self.id = 12345
            self.date = datetime.now()
            self.text = "Hello world"
            self.caption = None
            self.entities = []
            self.caption_entities = []
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
            self.from_user = None
            self.chat = None
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

    def setUp(self):
        """Set up test fixtures."""
        self.mock_message = self.SimpleMockMessage()

    def test_from_telegram_message_basic(self):
        """Test creating TelegramMessage from basic Telegram message."""

        class MockUser:
            def __init__(self):
                self.id = 12345
                self.is_bot = False
                self.first_name = "John"
                self.last_name = "Doe"
                self.username = "johndoe"
                self.language_code = "en"

        class MockChat:
            def __init__(self):
                self.id = 67890
                self.type = "private"
                self.first_name = "John"
                self.last_name = "Doe"

        self.mock_message.from_user = MockUser()
        self.mock_message.chat = MockChat()

        message = TelegramMessage.from_telegram_message(self.mock_message)

        assert message.message_id == 12345
        assert message.from_user is not None
        assert message.from_user.id == 12345
        assert message.chat is not None
        assert message.chat.id == 67890
        assert message.text == "Hello world"
        assert not message.has_media
        assert message.has_text

    def test_from_telegram_message_with_photo(self):
        """Test creating TelegramMessage with photo media."""

        class MockPhotoSize:
            def __init__(self, file_id: str, width: int, height: int):
                self.file_id = file_id
                self.width = width
                self.height = height

        self.mock_message.photo = [
            MockPhotoSize("photo1", 100, 100),
            MockPhotoSize("photo2", 200, 200),
        ]

        message = TelegramMessage.from_telegram_message(self.mock_message)

        assert message.media_type == MediaType.PHOTO
        assert message.has_media
        assert message.photo is not None
        assert len(message.photo) == 2

    def test_from_telegram_message_with_entities(self):
        """Test creating TelegramMessage with entities."""

        class MockEntity:
            def __init__(self, entity_type: str, offset: int, length: int):
                self.type = entity_type
                self.offset = offset
                self.length = length

        self.mock_message.entities = [MockEntity("url", 0, 10), MockEntity("bold", 10, 5)]

        message = TelegramMessage.from_telegram_message(self.mock_message)

        assert len(message.entities) == 2
        assert message.entities[0].type == MessageEntityType.URL
        assert message.entities[1].type == MessageEntityType.BOLD

    def test_from_telegram_message_forwarded(self):
        """Test channel forward — is_forwarded flag and forward_from_chat."""

        class MockForwardChat:
            def __init__(self):
                self.id = -10012345
                self.type = "channel"
                self.title = "Test Channel"

        self.mock_message.forward_from_chat = MockForwardChat()
        self.mock_message.forward_from_message_id = 54321

        message = TelegramMessage.from_telegram_message(self.mock_message)

        assert message.is_forwarded
        assert message.forward_from_chat is not None
        assert message.forward_from_chat.id == -10012345
        assert message.forward_from_message_id == 54321

    def test_from_telegram_message_forwarded_with_reply_markup(self):
        """Forwarded messages with inline buttons should parse without crashing."""

        class MockForwardChat:
            def __init__(self):
                self.id = -10012345
                self.type = "channel"
                self.title = "Test Channel"

        class MockButton:
            def __init__(self, text: str, url: str):
                self.text = text
                self.url = url

        class MockReplyMarkup:
            def __init__(self, inline_keyboard):
                self.inline_keyboard = inline_keyboard

        self.mock_message.text = "Forwarded with buttons"
        self.mock_message.forward_from_chat = MockForwardChat()
        self.mock_message.forward_from_message_id = 54321
        self.mock_message.reply_markup = MockReplyMarkup(
            [[MockButton("Test", "https://example.com")]]
        )

        message = TelegramMessage.from_telegram_message(self.mock_message)

        assert message.is_forwarded
        assert isinstance(message.reply_markup, dict)
        assert message.reply_markup["inline_keyboard"][0][0]["text"] == "Test"

    def test_from_telegram_message_forwarded_with_linked_chat_cycle(self):
        """Ensure channel forwards don't crash when Chat.linked_chat forms a cycle."""
        import types

        def _make_chat(*, chat_id: int, chat_type: ChatType, title: str):
            return types.SimpleNamespace(
                id=chat_id,
                type=chat_type,
                title=title,
                linked_chat=None,
            )

        channel = _make_chat(chat_id=-1001, chat_type=ChatType.CHANNEL, title="Test Channel")
        discussion = _make_chat(
            chat_id=-2001, chat_type=ChatType.SUPERGROUP, title="Test Discussion"
        )
        channel.linked_chat = discussion
        discussion.linked_chat = channel

        self.mock_message.forward_from_chat = channel
        self.mock_message.forward_from_message_id = 54321

        message = TelegramMessage.from_telegram_message(self.mock_message)

        assert message.is_forwarded
        assert message.forward_from_chat is not None
        assert message.forward_from_chat.id == -1001
        assert message.forward_from_message_id == 54321

    def test_from_telegram_message_forwarded_sender_name(self):
        """Privacy-protected forwards (forward_sender_name only) are detected as forwarded."""
        self.mock_message.text = "Hidden sender forward"
        self.mock_message.forward_sender_name = "Hidden User"
        self.mock_message.forward_date = datetime.now()

        message = TelegramMessage.from_telegram_message(self.mock_message)

        assert message.is_forwarded
        assert message.forward_sender_name == "Hidden User"
        assert message.forward_from is None
        assert message.forward_from_chat is None

    def test_from_telegram_message_forwarded_from_user(self):
        """User forwards (no channel) are detected as forwarded."""

        class MockUser:
            def __init__(self):
                self.id = 99999
                self.is_bot = False
                self.first_name = "Jane"
                self.last_name = "Doe"
                self.username = "janedoe"
                self.language_code = "en"

        self.mock_message.text = "User forward"
        self.mock_message.forward_from = MockUser()

        message = TelegramMessage.from_telegram_message(self.mock_message)

        assert message.is_forwarded
        assert message.forward_from is not None
        assert message.forward_from.id == 99999
        assert message.forward_from_chat is None

    def test_validate_basic(self):
        """Test basic message validation."""
        message = TelegramMessage(
            message_id=12345,
            from_user=TelegramUser(
                id=12345,
                is_bot=False,
                first_name="John",
            ),
            text="Hello world",
        )

        errors = message.validate_message()
        assert len(errors) == 0

    def test_validate_missing_message_id(self):
        """Test validation with missing message ID."""
        message = TelegramMessage(
            message_id=0,
            text="Hello world",
        )

        errors = message.validate_message()
        assert "Message ID is required" in errors

    def test_validate_missing_content(self):
        """Test validation with missing content."""
        message = TelegramMessage(
            message_id=12345,
        )

        errors = message.validate_message()
        assert "Message must have text, caption, or media content" in errors

    def test_validate_entity_bounds(self):
        """Test validation of entity bounds."""
        message = TelegramMessage(
            message_id=12345,
            text="Hello",
            entities=[
                MessageEntity(
                    type=MessageEntityType.BOLD,
                    offset=0,
                    length=10,  # Extends beyond text length
                )
            ],
        )

        errors = message.validate_message()
        assert "Entity extends beyond text length" in errors

    def test_get_effective_text(self):
        """Test getting effective text content."""
        message = TelegramMessage(message_id=12345, text="Hello world")
        assert message.get_effective_text() == "Hello world"

        message = TelegramMessage(message_id=12345, caption="Hello caption")
        assert message.get_effective_text() == "Hello caption"

        message = TelegramMessage(message_id=12345)
        assert message.get_effective_text() is None

    def test_get_effective_entities(self):
        """Test getting effective entities."""
        message = TelegramMessage(
            message_id=12345,
            text="Hello world",
            entities=[MessageEntity(type=MessageEntityType.BOLD, offset=0, length=5)],
        )
        assert len(message.get_effective_entities()) == 1

        message = TelegramMessage(
            message_id=12345,
            caption="Hello caption",
            caption_entities=[MessageEntity(type=MessageEntityType.ITALIC, offset=0, length=5)],
        )
        assert len(message.get_effective_entities()) == 1

    def test_is_command(self):
        """Test command detection."""
        message = TelegramMessage(
            message_id=12345,
            text="/start hello",
            entities=[MessageEntity(type=MessageEntityType.BOT_COMMAND, offset=0, length=6)],
        )
        assert message.is_command()

        message = TelegramMessage(
            message_id=12345,
            text="Hello world",
            entities=[MessageEntity(type=MessageEntityType.BOLD, offset=0, length=5)],
        )
        assert not message.is_command()

    def test_get_command(self):
        """Test command extraction."""
        message = TelegramMessage(
            message_id=12345,
            text="/start hello",
            entities=[MessageEntity(type=MessageEntityType.BOT_COMMAND, offset=0, length=6)],
        )
        assert message.get_command() == "/start"

    def test_get_urls(self):
        """Test URL extraction."""
        message = TelegramMessage(
            message_id=12345,
            text="Check out https://example.com and https://test.com",
            entities=[
                MessageEntity(type=MessageEntityType.URL, offset=10, length=19),
                MessageEntity(type=MessageEntityType.URL, offset=34, length=16),
            ],
        )
        urls = message.get_urls()
        assert len(urls) == 2
        assert "https://example.com" in urls
        assert "https://test.com" in urls

    def test_get_media_info(self):
        """Test media information retrieval."""
        message = TelegramMessage(
            message_id=12345,
            media_type=MediaType.PHOTO,
            photo=[{"file_id": "photo1", "width": 100, "height": 100}],
        )
        media_info = message.get_media_info()
        assert media_info is not None
        assert len(media_info) == 1

        message = TelegramMessage(message_id=12345, text="Hello world")
        assert message.get_media_info() is None

    def test_to_dict(self):
        """Test conversion to dictionary."""
        message = TelegramMessage(
            message_id=12345,
            from_user=TelegramUser(id=12345, is_bot=False, first_name="John"),
            text="Hello world",
            media_type=MediaType.PHOTO,
        )

        data = message.to_dict()
        assert isinstance(data, dict)
        assert data["message_id"] == 12345
        assert data["text"] == "Hello world"
        assert data["media_type"] == "photo"
        assert "from_user" in data


if __name__ == "__main__":
    unittest.main()
