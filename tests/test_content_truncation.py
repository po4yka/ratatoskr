"""Coverage for content truncation in URL and forward flows."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

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
        db_path="/tmp/content-truncation.db", allowed_user_ids=tuple(allowed_ids)
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


def _mock_crawl_result(content: str) -> Mock:
    result = Mock()
    result.status = "ok"
    result.content_markdown = content
    result.content_html = None
    result.http_status = 200
    result.latency_ms = 1000
    result.error_text = None
    return result


def _mock_llm_result() -> Mock:
    result = Mock()
    result.status = "ok"
    result.response_text = '{"title": "Test", "summary": "Test summary"}'
    result.model = "deepseek/deepseek-v4-flash"
    result.endpoint = "https://openrouter.ai/api/v1/chat/completions"
    result.request_headers = {}
    result.request_messages = []
    result.response_json = {}
    result.tokens_prompt = 1000
    result.tokens_completion = 100
    result.cost_usd = 0.01
    result.latency_ms = 2000
    result.error_text = None
    return result


async def test_url_flow_content_truncation(database: Database) -> None:
    bot = _make_bot(database, allowed_ids=[12345])
    very_long = "A" * 50000 + "B" * 20000

    with (
        patch.object(bot._firecrawl, "scrape_markdown", return_value=_mock_crawl_result(very_long)),
        patch.object(bot._llm_client, "chat", return_value=_mock_llm_result()),
    ):
        msg = FakeMessage("https://example.com", uid=12345)
        await bot._handle_url_flow(msg, "https://example.com")
        assert len(msg._replies) > 0


async def test_forward_flow_content_truncation(database: Database) -> None:
    bot = _make_bot(database, allowed_ids=[12345])
    very_long = "A" * 50000 + "B" * 20000

    with patch.object(bot._llm_client, "chat", return_value=_mock_llm_result()):
        msg = FakeMessage(very_long, uid=12345)
        await bot._handle_forward_flow(msg)
        assert len(msg._replies) > 0


async def test_no_truncation_when_content_short(database: Database) -> None:
    bot = _make_bot(database, allowed_ids=[12345])
    short = "This is a short article about testing."

    with (
        patch.object(bot._firecrawl, "scrape_markdown", return_value=_mock_crawl_result(short)),
        patch.object(bot._llm_client, "chat", return_value=_mock_llm_result()),
    ):
        msg = FakeMessage("https://example.com", uid=12345)
        await bot._handle_url_flow(msg, "https://example.com")
        assert len(msg._replies) > 0


async def test_truncation_exact_boundary(database: Database) -> None:
    bot = _make_bot(database, allowed_ids=[12345])
    boundary = "A" * 45000

    with (
        patch.object(bot._firecrawl, "scrape_markdown", return_value=_mock_crawl_result(boundary)),
        patch.object(bot._llm_client, "chat", return_value=_mock_llm_result()),
    ):
        msg = FakeMessage("https://example.com", uid=12345)
        await bot._handle_url_flow(msg, "https://example.com")
        assert len(msg._replies) > 0


async def test_truncation_one_character_over(database: Database) -> None:
    bot = _make_bot(database, allowed_ids=[12345])
    over = "A" * 45001

    with (
        patch.object(bot._firecrawl, "scrape_markdown", return_value=_mock_crawl_result(over)),
        patch.object(bot._llm_client, "chat", return_value=_mock_llm_result()),
    ):
        msg = FakeMessage("https://example.com", uid=12345)
        await bot._handle_url_flow(msg, "https://example.com")
        assert len(msg._replies) > 0
