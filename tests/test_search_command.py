"""Coverage for the /search command and the search-service initialization wiring.

Ported off the legacy DatabaseSessionManager + tests.db_helpers shim.
The bot is constructed against async Postgres; the hybrid search service
is replaced per-test with a FakeHybridSearch instance so /search routing
is exercised without standing up Qdrant + the real search graph. The
interaction-tracking assertion now reads via the async helper.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from app.adapters.telegram.telegram_bot import TelegramBot
from app.application.services.topic_search import TopicArticle
from tests.conftest import make_test_app_config
from tests.db_helpers_async import get_user_interactions
from tests.telegram_bot_builders import AUDIT_REPOSITORY_BUILDER, RUNTIME_BUILDER

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.session import Database


class FakeMessage:
    """Mock Telegram message for testing."""

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
    """Test spy for TelegramBot that tracks behaviour."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen_urls: list[str] = []

    async def _handle_url_flow(self, message: Any, url_text: str, **_: object) -> None:
        self.seen_urls.append(url_text)
        await self._safe_reply(message, f"OK {url_text}")


def _make_bot(database: Database) -> BotSpy:
    cfg = make_test_app_config(db_path="/tmp/search-test.db", allowed_user_ids=(1, 42))
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
# /search command behaviour
# ---------------------------------------------------------------------------


async def test_search_command_with_results(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    class FakeHybridSearch:
        def __init__(self) -> None:
            self.queries: list[tuple[str, str | None]] = []

        async def search(
            self,
            query: str,
            *,
            filters: object = None,
            correlation_id: str | None = None,
        ) -> list[TopicArticle]:
            self.queries.append((query, correlation_id))
            return [
                TopicArticle(
                    title="Machine Learning Fundamentals",
                    url="https://example.com/ml-fundamentals",
                    snippet="An introduction to machine learning concepts and algorithms.",
                    source="ML Weekly",
                    published_at="2024-01-15",
                ),
                TopicArticle(
                    title="Deep Learning with Python",
                    url="https://example.com/deep-learning-python",
                    snippet="Practical guide to building neural networks with Python.",
                    source="Tech Blog",
                    published_at="2024-02-20",
                ),
                TopicArticle(
                    title="AI Ethics and Safety",
                    url="https://example.com/ai-ethics",
                    snippet="Exploring ethical considerations in artificial intelligence.",
                    source="AI Journal",
                    published_at="2024-03-10",
                ),
            ]

    fake_search = FakeHybridSearch()
    bot.hybrid_search_service = fake_search
    bot.message_handler.command_processor.hybrid_search = fake_search

    msg = FakeMessage("/search machine learning")
    await bot._on_message(msg)

    assert fake_search.queries
    assert fake_search.queries[0][0] == "machine learning"

    replies = " ".join(msg._replies)
    assert "Search Results" in replies
    assert "machine learning" in replies
    assert "Found 3 article(s)" in replies
    assert "Machine Learning Fundamentals" in replies
    assert "Deep Learning with Python" in replies
    assert "AI Ethics and Safety" in replies
    assert "https://example.com/ml-fundamentals" in replies


async def test_search_command_no_results(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    class FakeHybridSearch:
        async def search(
            self,
            query: str,
            *,
            filters: object = None,
            correlation_id: str | None = None,
        ) -> list[TopicArticle]:
            return []

    fake_search = FakeHybridSearch()
    bot.hybrid_search_service = fake_search
    bot.message_handler.command_processor.hybrid_search = fake_search

    msg = FakeMessage("/search nonexistent topic xyz")
    await bot._on_message(msg)

    replies = " ".join(msg._replies)
    assert "No articles found" in replies
    assert "nonexistent topic xyz" in replies
    assert "Broader search terms" in replies
    assert "/find" in replies


async def test_search_command_without_query(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    class FakeHybridSearch:
        async def search(
            self,
            query: str,
            *,
            filters: object = None,
            correlation_id: str | None = None,
        ) -> list[TopicArticle]:
            msg = "Should not be called when query is missing"
            raise AssertionError(msg)

    fake_search = FakeHybridSearch()
    bot.hybrid_search_service = fake_search
    bot.message_handler.command_processor.hybrid_search = fake_search

    msg = FakeMessage("/search")
    await bot._on_message(msg)

    replies = " ".join(msg._replies)
    assert "Usage:" in replies
    assert "/search <query>" in replies
    assert "Examples:" in replies
    assert "machine learning" in replies
    assert "Semantic vector search" in replies
    assert "Keyword (FTS) search" in replies


async def test_search_command_service_unavailable(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    bot.hybrid_search_service = None
    bot.message_handler.command_processor.hybrid_search = None

    msg = FakeMessage("/search test query")
    await bot._on_message(msg)

    replies = " ".join(msg._replies)
    assert "Semantic search is currently unavailable" in replies


async def test_search_command_with_error(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    class FakeHybridSearch:
        async def search(
            self,
            query: str,
            *,
            filters: object = None,
            correlation_id: str | None = None,
        ) -> list[TopicArticle]:
            raise RuntimeError("Simulated search error")

    fake_search = FakeHybridSearch()
    bot.hybrid_search_service = fake_search
    bot.message_handler.command_processor.hybrid_search = fake_search

    msg = FakeMessage("/search error test")
    await bot._on_message(msg)

    replies = " ".join(msg._replies)
    assert "Search failed" in replies
    assert "try again" in replies.lower()


async def test_search_command_truncates_long_titles(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    class FakeHybridSearch:
        async def search(
            self,
            query: str,
            *,
            filters: object = None,
            correlation_id: str | None = None,
        ) -> list[TopicArticle]:
            return [
                TopicArticle(
                    title="A" * 150,
                    url="https://example.com/long",
                    snippet="B" * 200,
                    source="Example",
                    published_at="2024-01-01",
                )
            ]

    fake_search = FakeHybridSearch()
    bot.hybrid_search_service = fake_search
    bot.message_handler.command_processor.hybrid_search = fake_search

    msg = FakeMessage("/search test")
    await bot._on_message(msg)

    replies = " ".join(msg._replies)
    assert "..." in replies
    assert "A" * 97 in replies
    assert "B" * 147 in replies


async def test_search_command_displays_metadata(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    class FakeHybridSearch:
        async def search(
            self,
            query: str,
            *,
            filters: object = None,
            correlation_id: str | None = None,
        ) -> list[TopicArticle]:
            return [
                TopicArticle(
                    title="Article with Metadata",
                    url="https://example.com/article",
                    snippet="Test article",
                    source="Tech News Daily",
                    published_at="2024-06-15",
                ),
                TopicArticle(
                    title="Article without Metadata",
                    url="https://example.com/article2",
                    snippet="Another test",
                    source=None,
                    published_at=None,
                ),
            ]

    fake_search = FakeHybridSearch()
    bot.hybrid_search_service = fake_search
    bot.message_handler.command_processor.hybrid_search = fake_search

    msg = FakeMessage("/search metadata test")
    await bot._on_message(msg)

    replies = " ".join(msg._replies)
    assert "Tech News Daily" in replies
    assert "2024-06-15" in replies
    assert "📰" in replies
    assert "📅" in replies


async def test_search_command_limits_to_ten_results(database: Database) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    class FakeHybridSearch:
        async def search(
            self,
            query: str,
            *,
            filters: object = None,
            correlation_id: str | None = None,
        ) -> list[TopicArticle]:
            return [
                TopicArticle(
                    title=f"Article {i}",
                    url=f"https://example.com/article{i}",
                    snippet=f"Snippet {i}",
                    source=None,
                    published_at=None,
                )
                for i in range(1, 16)
            ]

    fake_search = FakeHybridSearch()
    bot.hybrid_search_service = fake_search
    bot.message_handler.command_processor.hybrid_search = fake_search

    msg = FakeMessage("/search many results")
    await bot._on_message(msg)

    replies = " ".join(msg._replies)
    assert "Found 15 article(s)" in replies
    assert "Article 1" in replies
    assert "Article 10" in replies
    assert "Article 11" not in replies
    assert "Article 15" not in replies


async def test_search_command_interaction_tracking(
    database: Database, session: AsyncSession
) -> None:
    bot = _make_bot(database)
    bot.response_formatter.MIN_MESSAGE_INTERVAL_MS = 0

    class FakeHybridSearch:
        async def search(
            self,
            query: str,
            *,
            filters: object = None,
            correlation_id: str | None = None,
        ) -> list[TopicArticle]:
            return [
                TopicArticle(
                    title="Test Article",
                    url="https://example.com/test",
                    snippet="Test snippet",
                    source=None,
                    published_at=None,
                )
            ]

    fake_search = FakeHybridSearch()
    bot.hybrid_search_service = fake_search
    bot.message_handler.command_processor.hybrid_search = fake_search

    msg = FakeMessage("/search interaction test", uid=42)
    await bot._on_message(msg)

    interactions = await get_user_interactions(session, uid=42, limit=10)
    assert len(interactions) > 0
    last_interaction = interactions[0]
    assert last_interaction["command"] == "/search"
    assert last_interaction["response_type"] == "search_results"
    assert last_interaction["response_sent"] is True


# ---------------------------------------------------------------------------
# Service initialisation wiring
# ---------------------------------------------------------------------------


async def test_search_services_initialized_on_bot_creation(database: Database) -> None:
    bot = _make_bot(database)

    assert bot.embedding_service is not None
    assert bot.vector_search_service is not None
    assert bot.query_expansion_service is not None
    assert bot.hybrid_search_service is not None

    assert bot.message_handler.command_processor.hybrid_search is not None
    assert bot.message_handler.command_processor.hybrid_search == bot.hybrid_search_service


async def test_search_service_parameters(database: Database) -> None:
    bot = _make_bot(database)

    assert bot.query_expansion_service._max_expansions == 5
    assert bot.query_expansion_service._use_synonyms is True

    assert bot.hybrid_search_service._fts_weight == 0.4
    assert bot.hybrid_search_service._vector_weight == 0.6
