"""Coverage for /help, /summarize, /cancel, /dbinfo, /dbverify, /find* commands.

Ported off the legacy DatabaseSessionManager + tests.db_helpers shim. Each
test constructs a real TelegramBot wired against async Postgres and the
function-scoped session/database fixtures, then drives commands through
bot._on_message. URL processing is short-circuited via a BotSpy that
replaces url_processor.handle_url_flow so the tests stay focused on
command routing rather than the summarisation pipeline (which has its
own coverage in test_dedupe.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from app.adapters.telegram.telegram_bot import TelegramBot
from app.application.services.topic_search import TopicArticle
from tests.conftest import make_test_app_config
from tests.db_helpers_async import (
    create_request,
    insert_audit_log,
    insert_crawl_result,
    insert_summary,
)
from tests.telegram_bot_builders import AUDIT_REPOSITORY_BUILDER, RUNTIME_BUILDER

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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
        self.id = 123
        self.message_id = 123

    async def reply_text(self, text: str, parse_mode: str | None = None) -> None:
        _ = parse_mode
        self._replies.append(text)


class BotSpy(TelegramBot):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen_urls: list[str] = []

        if hasattr(self, "url_processor"):

            async def mock_handle_url_flow(message: Any, url_text: str, **_: object) -> None:
                self.seen_urls.append(url_text)
                await self._safe_reply(message, f"OK {url_text}")

            self.url_processor.handle_url_flow = mock_handle_url_flow

    async def _handle_url_flow(self, message: Any, url_text: str, **_: object) -> None:
        self.seen_urls.append(url_text)
        await self._safe_reply(message, f"OK {url_text}")


def _make_bot(database: Database) -> BotSpy:
    cfg = make_test_app_config(db_path="/tmp/cmd-test.db", allowed_user_ids=(1, 42))
    from app.adapters import telegram_bot as tbmod

    tbmod.Client = object
    tbmod.filters = None

    with patch("app.adapters.openrouter.openrouter_client.OpenRouterClient") as mock_or:
        mock_or.return_value = AsyncMock()
        return BotSpy(
            cfg=cfg,
            db=database,
            runtime_builder=RUNTIME_BUILDER,
            audit_repository_builder=AUDIT_REPOSITORY_BUILDER,
        )


# ---------------------------------------------------------------------------
# Basic command routing
# ---------------------------------------------------------------------------


async def test_help(database: Database) -> None:
    bot = _make_bot(database)
    msg = FakeMessage("/help")
    await bot._on_message(msg)
    assert any("Commands" in r for r in msg._replies)


async def test_summarize_same_message(database: Database) -> None:
    bot = _make_bot(database)
    url = "https://example.com/a"
    msg = FakeMessage(f"/summarize {url}")
    await bot._on_message(msg)
    assert url in bot.seen_urls


async def test_summarize_next_message(database: Database) -> None:
    bot = _make_bot(database)
    uid = 42
    await bot._on_message(FakeMessage("/summarize", uid=uid))
    assert uid in bot._awaiting_url_users  # type: ignore[attr-defined]
    url = "https://example.com/b"
    await bot._on_message(FakeMessage(url, uid=uid))
    assert url in bot.seen_urls
    assert uid not in bot._awaiting_url_users  # type: ignore[attr-defined]


async def test_cancel_awaiting_request(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0
    uid = 42

    await bot._on_message(FakeMessage("/summarize", uid=uid))
    assert uid in bot._awaiting_url_users  # type: ignore[attr-defined]

    cancel_msg = FakeMessage("/cancel", uid=uid)
    await bot._on_message(cancel_msg)

    assert uid not in bot._awaiting_url_users  # type: ignore[attr-defined]
    assert any("Cancelled your pending URL request" in reply for reply in cancel_msg._replies)


async def test_cancel_after_multi_links_direct_processing(database: Database) -> None:
    """After multi-link direct processing, /cancel reports nothing to cancel."""
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0
    uid = 42
    multi_text = "https://example.com/a\nhttps://example.com/b"

    await bot._on_message(FakeMessage(multi_text, uid=uid))

    cancel_msg = FakeMessage("/cancel", uid=uid)
    await bot._on_message(cancel_msg)

    assert any("No pending link requests" in reply for reply in cancel_msg._replies)


async def test_cancel_without_pending_requests(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0
    uid = 42

    cancel_msg = FakeMessage("/cancel", uid=uid)
    await bot._on_message(cancel_msg)

    assert any("No pending link requests" in reply for reply in cancel_msg._replies)


async def test_cancel_includes_active_requests(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0
    uid = 42

    bot.message_handler.task_manager.cancel = AsyncMock(return_value=2)

    cancel_msg = FakeMessage("/cancel", uid=uid)
    await bot._on_message(cancel_msg)

    bot.message_handler.task_manager.cancel.assert_awaited_once_with(uid, exclude_current=True)
    assert any("ongoing requests" in reply for reply in cancel_msg._replies)


# ---------------------------------------------------------------------------
# /dbinfo + /dbverify
# ---------------------------------------------------------------------------


async def test_dbinfo_command(database: Database, session: AsyncSession) -> None:
    bot = _make_bot(database)
    request_id = await create_request(
        session,
        type_="url",
        status="completed",
        correlation_id="cid",
        chat_id=1,
        user_id=1,
        input_url="https://example.com",
        normalized_url="https://example.com",
    )
    await insert_summary(session, request_id=request_id, lang="en", json_payload="{}")
    await insert_audit_log(session, level="INFO", event="test", details_json="{}")
    await session.commit()

    msg = FakeMessage("/dbinfo")
    await bot._on_message(msg)
    assert any("Database Overview" in reply for reply in msg._replies)
    assert any("Requests by status" in reply for reply in msg._replies)
    assert any("Totals" in reply for reply in msg._replies)


async def test_dbverify_command(database: Database, session: AsyncSession) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    base_summary = {
        "summary_250": "Short summary.",
        "summary_1000": "Medium summary.",
        "tldr": "Long summary.",
        "key_ideas": ["Idea"],
        "topic_tags": ["#tag"],
        "entities": {"people": [], "organizations": [], "locations": []},
        "estimated_reading_time_min": 5,
        "key_stats": [],
        "answered_questions": [],
        "readability": {"method": "FK", "score": 50.0, "level": "Standard"},
        "seo_keywords": [],
        "metadata": {
            "title": "Title",
            "canonical_url": "https://example.com/article",
            "domain": "example.com",
            "author": "Author",
            "published_at": "2024-01-01",
            "last_updated": "2024-01-01",
        },
        "extractive_quotes": [],
        "highlights": [],
        "questions_answered": [],
        "categories": [],
        "topic_taxonomy": [],
        "hallucination_risk": "low",
        "confidence": 1.0,
        "forwarded_post_extras": None,
        "key_points_to_remember": [],
    }

    rid_good = await create_request(
        session,
        type_="url",
        status="ok",
        correlation_id="good",
        chat_id=1,
        user_id=1,
        input_url="https://example.com/good",
        normalized_url="https://example.com/good",
        route_version=1,
    )
    await insert_summary(session, request_id=rid_good, lang="en", json_payload=base_summary)
    await insert_crawl_result(
        session,
        request_id=rid_good,
        source_url="https://example.com/good",
        endpoint="/v2/scrape",
        http_status=200,
        status="ok",
        options_json={},
        correlation_id="fc-good",
        content_markdown="# md",
        content_html=None,
        structured_json={},
        metadata_json={},
        links_json=["https://example.com/other"],
        screenshots_paths_json=None,
        firecrawl_success=True,
        firecrawl_error_code=None,
        firecrawl_error_message=None,
        firecrawl_details_json=None,
        raw_response_json=None,
        latency_ms=100,
        error_text=None,
    )
    await session.commit()

    msg = FakeMessage("/dbverify")
    await bot._on_message(msg)

    assert any("Database Verification" in reply for reply in msg._replies)


# ---------------------------------------------------------------------------
# /findweb /find /finddb
# ---------------------------------------------------------------------------


async def test_findweb_command_success(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    class FakeSearch:
        def __init__(self) -> None:
            self.queries: list[tuple[str, str | None]] = []

        async def find_articles(
            self, topic: str, *, correlation_id: str | None = None
        ) -> list[TopicArticle]:
            self.queries.append((topic, correlation_id))
            return [
                TopicArticle(
                    title="Android System Design Overview",
                    url="https://example.com/android-design",
                    snippet="Key considerations for the Android system architecture.",
                    source="Example Weekly",
                    published_at="2024-04-01",
                ),
                TopicArticle(
                    title="Scaling Android Services",
                    url="https://example.com/android-services",
                    snippet="How large teams approach Android service scalability.",
                    source=None,
                    published_at=None,
                ),
            ]

    fake_search = FakeSearch()
    bot.topic_searcher = fake_search
    bot.message_handler.command_processor.topic_searcher = fake_search

    msg = FakeMessage("/findweb Android System Design")
    await bot._on_message(msg)

    assert fake_search.queries
    assert fake_search.queries[0][0] == "Android System Design"
    assert any("Online search results" in reply for reply in msg._replies)
    assert any("summarize" in reply.lower() for reply in msg._replies)


async def test_find_alias_uses_online_search(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    class FakeSearch:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def find_articles(
            self, topic: str, *, correlation_id: str | None = None
        ) -> list[TopicArticle]:
            self.queries.append(topic)
            return []

    fake_search = FakeSearch()
    bot.topic_searcher = fake_search
    bot.message_handler.command_processor.topic_searcher = fake_search

    msg = FakeMessage("/find Android")
    await bot._on_message(msg)

    assert fake_search.queries == ["Android"]
    assert any("No recent online articles" in reply for reply in msg._replies)


async def test_finddb_command_success(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    class FakeLocalSearch:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def find_articles(
            self, topic: str, *, correlation_id: str | None = None
        ) -> list[TopicArticle]:
            self.queries.append(topic)
            return [
                TopicArticle(
                    title="Saved Android System Design",
                    url="https://example.com/android-design",
                    snippet="Local summary about Android system design.",
                    source="example.com",
                    published_at="2024-04-01",
                )
            ]

    fake_local = FakeLocalSearch()
    bot.local_searcher = fake_local
    bot.message_handler.command_processor.local_searcher = fake_local

    msg = FakeMessage("/finddb Android System Design")
    await bot._on_message(msg)

    assert fake_local.queries == ["Android System Design"]
    assert any("Saved library results" in reply for reply in msg._replies)
    assert any("summarize" in reply.lower() for reply in msg._replies)


async def test_find_commands_require_topic(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    msg_web = FakeMessage("/findweb")
    await bot._on_message(msg_web)

    class StubLocalSearch:
        async def find_articles(
            self, topic: str, *, correlation_id: str | None = None
        ) -> list[TopicArticle]:
            msg = "Should not be called when topic missing"
            raise AssertionError(msg)

    stub = StubLocalSearch()
    bot.local_searcher = stub
    bot.message_handler.command_processor.local_searcher = stub

    msg_db = FakeMessage("/finddb")
    await bot._on_message(msg_db)

    assert any("Usage" in reply for reply in msg_web._replies)
    assert any("Usage" in reply for reply in msg_db._replies)
