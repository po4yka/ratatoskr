"""Coverage for multi-link direct processing and document URL ingestion."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from app.adapters.telegram.telegram_bot import TelegramBot
from app.config.runtime import RuntimeConfig
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
        self.id = 777
        self.message_id = 777

    async def reply_text(self, text: str) -> None:
        self._replies.append(text)


class SpyBot(TelegramBot):
    def __post_init__(self) -> None:
        with patch("app.adapters.openrouter.openrouter_client.OpenRouterClient") as mock_or:
            mock_or.return_value = AsyncMock()
            super().__post_init__()
        self.seen_urls: list[str] = []

        if hasattr(self, "url_processor"):

            async def mock_handle_url_flow(message: Any, url_text: str, **_: object) -> None:
                self.seen_urls.append(url_text)
                await self._safe_reply(message, f"OK {url_text}")

            self.url_processor.handle_url_flow = mock_handle_url_flow

    async def _handle_url_flow(self, message: Any, url_text: str, **_: object) -> None:
        self.seen_urls.append(url_text)
        await self._safe_reply(message, f"OK {url_text}")


def _make_bot(database: Database) -> SpyBot:
    cfg = make_test_app_config(
        db_path="/tmp/multi-links.db",
        allowed_user_ids=(1, 55, 66, 77, 88),
        runtime=RuntimeConfig(
            db_path="/tmp/multi-links.db",
            log_level="INFO",
            request_timeout_sec=5,
            preferred_lang="en",
            debug_payloads=False,
            aggregation_bundle_enabled=False,
        ),
    )
    from app.adapters import telegram_bot as tbmod

    tbmod.Client = object
    tbmod.filters = None
    return SpyBot(
        cfg=cfg,
        db=database,
        runtime_builder=RUNTIME_BUILDER,
        audit_repository_builder=AUDIT_REPOSITORY_BUILDER,
    )


async def test_direct_process_multi_links(database: Database) -> None:
    bot = _make_bot(database)
    text = "Here are two links:\nhttps://a.example/a\nhttps://b.example/b"
    await bot._on_message(FakeMessage(text, uid=55))
    assert "https://a.example/a" in bot.seen_urls
    assert "https://b.example/b" in bot.seen_urls


async def test_cancel_after_direct_multi_links(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0
    text = "https://a.example/a\nhttps://b.example/b\nhttps://a.example/a"  # duplicate
    uid = 66
    await bot._on_message(FakeMessage(text, uid=uid))
    assert len(bot.seen_urls) > 0

    cancel_msg = FakeMessage("/cancel", uid=uid)
    await bot._on_message(cancel_msg)
    assert any("No pending link requests" in r for r in cancel_msg._replies)


async def test_document_file_processing(database: Database) -> None:
    bot = _make_bot(database)
    test_urls = [
        "https://example1.com/article1",
        "https://example2.com/article2",
        "https://example3.com/article3",
    ]

    class MockDocument:
        def __init__(self, file_name: str) -> None:
            self.file_name = file_name

    class MockDocumentMessage(FakeMessage):
        def __init__(self, file_name: str, uid: int = 1) -> None:
            super().__init__("", uid)
            self.document = MockDocument(file_name)

        async def download(self) -> str:
            import tempfile

            with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
                for url in test_urls:
                    f.write(f"{url}\n")
                return f.name

    msg = MockDocumentMessage("urls.txt", uid=77)
    await bot._on_message(msg)

    assert len(bot.seen_urls) == len(test_urls)
    for url in test_urls:
        assert url in bot.seen_urls


async def test_invalid_document_file(database: Database) -> None:
    bot = _make_bot(database)

    class MockDocument:
        def __init__(self, file_name: str) -> None:
            self.file_name = file_name

    class MockDocumentMessage(FakeMessage):
        def __init__(self, file_name: str, uid: int = 1) -> None:
            super().__init__("", uid)
            self.document = MockDocument(file_name)

    msg = MockDocumentMessage("random_file.txt", uid=88)
    await bot._on_message(msg)
    assert len(bot.seen_urls) == 0
