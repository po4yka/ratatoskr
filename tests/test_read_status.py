"""Coverage for /unread and /read commands and the read-status helpers.

Ported off the legacy DatabaseSessionManager + tests.db_helpers shim.
The argument-parsing tests are pure-Python and unchanged. The helper
tests use async db_helpers + the session fixture; the command tests
construct a real TelegramBot wired to async Postgres and invoke
commands via bot._on_message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from app.adapters.telegram.command_dispatcher import TelegramCommandDispatcher
from app.adapters.telegram.telegram_bot import TelegramBot
from tests.conftest import make_test_app_config
from tests.db_helpers_async import (
    create_request,
    get_read_status,
    get_summary_by_request,
    get_unread_summaries,
    get_unread_summary_by_request_id,
    insert_summary,
    mark_summary_as_read,
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

    async def reply_text(self, text: str, **_kwargs: object) -> None:
        self._replies.append(text)


class ReadStatusBot(TelegramBot):
    def __post_init__(self) -> None:
        with patch("app.adapters.openrouter.openrouter_client.OpenRouterClient") as mock_or:
            mock_or.return_value = AsyncMock()
            super().__post_init__()
        self.seen_urls: list[str] = []

        if hasattr(self, "url_processor"):
            self.url_processor.handle_url_flow = self._fake_url_flow

    async def _handle_url_flow(self, message: Any, url_text: str, **_: object) -> None:
        self.seen_urls.append(url_text)
        await self._safe_reply(message, f"OK {url_text}")

    async def _fake_url_flow(self, message: Any, url_text: str, **_: object) -> None:
        self.seen_urls.append(url_text)
        await self._safe_reply(message, f"OK {url_text}")


def _make_bot(database: Database) -> ReadStatusBot:
    cfg = make_test_app_config(db_path="/tmp/read-status.db", allowed_user_ids=(1,))
    from app.adapters import telegram_bot as tbmod

    tbmod.Client = object
    tbmod.filters = None
    return ReadStatusBot(
        cfg=cfg,
        db=database,
        runtime_builder=RUNTIME_BUILDER,
        audit_repository_builder=AUDIT_REPOSITORY_BUILDER,
    )


# ---------------------------------------------------------------------------
# Pure-Python: /unread argument parser
# ---------------------------------------------------------------------------


class TestParseUnreadArguments:
    def test_parse_unread_with_mention_only(self) -> None:
        limit, topic = TelegramCommandDispatcher._parse_unread_arguments("/unread@bot")
        assert limit == 5
        assert topic is None

    def test_parse_unread_with_mention_and_limit(self) -> None:
        limit, topic = TelegramCommandDispatcher._parse_unread_arguments("/unread@bot 3")
        assert limit == 3
        assert topic is None

    def test_parse_unread_with_mention_and_topic(self) -> None:
        limit, topic = TelegramCommandDispatcher._parse_unread_arguments("/unread@bot gardening")
        assert limit == 5
        assert topic == "gardening"

    def test_parse_unread_with_numeric_topic_only(self) -> None:
        limit, topic = TelegramCommandDispatcher._parse_unread_arguments("/unread 2024")
        assert limit == 5
        assert topic == "2024"

    def test_parse_unread_with_numeric_topic_and_limit(self) -> None:
        limit, topic = TelegramCommandDispatcher._parse_unread_arguments("/unread 2024 limit=3")
        assert limit == 3
        assert topic == "2024"

    def test_parse_unread_with_topic_and_trailing_limit(self) -> None:
        limit, topic = TelegramCommandDispatcher._parse_unread_arguments("/unread ai 2")
        assert limit == 2
        assert topic == "ai"

    def test_parse_unread_trailing_limit_above_max_is_topic(self) -> None:
        limit, topic = TelegramCommandDispatcher._parse_unread_arguments("/unread ai 99")
        assert limit == 5
        assert topic == "ai 99"

    def test_parse_unread_numeric_only_without_mention_is_topic(self) -> None:
        limit, topic = TelegramCommandDispatcher._parse_unread_arguments("/unread 3")
        assert limit == 5
        assert topic == "3"

    def test_parse_unread_numeric_only_with_mention_is_limit(self) -> None:
        limit, topic = TelegramCommandDispatcher._parse_unread_arguments("/unread@bot 4")
        assert limit == 4
        assert topic is None


# ---------------------------------------------------------------------------
# DB-backed: read-status helpers
# ---------------------------------------------------------------------------


async def test_summary_read_status_defaults(session: AsyncSession) -> None:
    rid = await create_request(
        session,
        type_="url",
        status="pending",
        correlation_id=None,
        chat_id=None,
        user_id=None,
        normalized_url="https://example.com/test-defaults",
        route_version=1,
    )
    await insert_summary(session, request_id=rid, lang="en", json_payload={"title": "Test Article"})
    row = await get_summary_by_request(session, rid)
    assert row is not None
    assert row["is_read"] == 0  # boolean column rendered as 0/1 by helper


async def test_summary_read_status_explicit(session: AsyncSession) -> None:
    rid1 = await create_request(
        session,
        type_="url",
        status="pending",
        correlation_id=None,
        chat_id=None,
        user_id=None,
        normalized_url="https://example.com/explicit-1",
        route_version=1,
    )
    rid2 = await create_request(
        session,
        type_="url",
        status="pending",
        correlation_id=None,
        chat_id=None,
        user_id=None,
        normalized_url="https://example.com/explicit-2",
        route_version=1,
    )
    await insert_summary(
        session,
        request_id=rid1,
        lang="en",
        json_payload={"title": "Unread Article"},
        is_read=False,
    )
    await insert_summary(
        session,
        request_id=rid2,
        lang="en",
        json_payload={"title": "Read Article"},
        is_read=True,
    )
    row1 = await get_summary_by_request(session, rid1)
    row2 = await get_summary_by_request(session, rid2)
    assert row1 is not None and row1["is_read"] == 0
    assert row2 is not None and row2["is_read"] == 1


async def test_get_unread_summaries(session: AsyncSession) -> None:
    rid1 = await create_request(
        session,
        type_="url",
        status="pending",
        input_url="https://example1.com",
        normalized_url="https://example1.com",
        correlation_id=None,
        chat_id=None,
        user_id=None,
        route_version=1,
    )
    rid2 = await create_request(
        session,
        type_="url",
        status="pending",
        input_url="https://example2.com",
        normalized_url="https://example2.com",
        correlation_id=None,
        chat_id=None,
        user_id=None,
        route_version=1,
    )
    rid3 = await create_request(
        session,
        type_="url",
        status="pending",
        input_url="https://example3.com",
        normalized_url="https://example3.com",
        correlation_id=None,
        chat_id=None,
        user_id=None,
        route_version=1,
    )
    await insert_summary(
        session, request_id=rid1, lang="en", json_payload={"title": "Article 1"}, is_read=False
    )
    await insert_summary(
        session, request_id=rid2, lang="en", json_payload={"title": "Article 2"}, is_read=True
    )
    await insert_summary(
        session, request_id=rid3, lang="en", json_payload={"title": "Article 3"}, is_read=False
    )

    unread = await get_unread_summaries(session, limit=10)
    assert len(unread) == 2
    assert unread[0]["input_url"] == "https://example1.com"
    assert unread[1]["input_url"] == "https://example3.com"


async def test_get_unread_summaries_limit(session: AsyncSession) -> None:
    for i in range(5):
        rid = await create_request(
            session,
            type_="url",
            status="pending",
            input_url=f"https://example{i}.com",
            normalized_url=f"https://example{i}.com",
            correlation_id=None,
            chat_id=None,
            user_id=None,
            route_version=1,
        )
        await insert_summary(
            session,
            request_id=rid,
            lang="en",
            json_payload={"title": f"Article {i}"},
            is_read=False,
        )

    unread = await get_unread_summaries(session, limit=3)
    assert [row["input_url"] for row in unread] == [
        "https://example0.com",
        "https://example1.com",
        "https://example2.com",
    ]


async def test_get_unread_summaries_filters_by_user_and_chat(
    session: AsyncSession,
) -> None:
    rid_target = await create_request(
        session,
        type_="url",
        status="pending",
        input_url="https://visible.com",
        normalized_url="https://visible.com",
        correlation_id=None,
        chat_id=111,
        user_id=555,
        route_version=1,
    )
    rid_other_user = await create_request(
        session,
        type_="url",
        status="pending",
        input_url="https://other-user.com",
        normalized_url="https://other-user.com",
        correlation_id=None,
        chat_id=111,
        user_id=777,
        route_version=1,
    )
    rid_other_chat = await create_request(
        session,
        type_="url",
        status="pending",
        input_url="https://other-chat.com",
        normalized_url="https://other-chat.com",
        correlation_id=None,
        chat_id=222,
        user_id=555,
        route_version=1,
    )
    for rid in (rid_target, rid_other_user, rid_other_chat):
        await insert_summary(
            session,
            request_id=rid,
            lang="en",
            json_payload={"title": "Scoped Article"},
            is_read=False,
        )

    unread_scoped = await get_unread_summaries(session, user_id=555, chat_id=111, limit=10)
    assert len(unread_scoped) == 1
    assert unread_scoped[0]["input_url"] == "https://visible.com"


async def test_get_unread_summaries_topic_filter(session: AsyncSession) -> None:
    payloads = (
        {
            "title": "AI breakthroughs",
            "topic_tags": ["Artificial Intelligence", "Research"],
            "metadata": {"description": "Advances in AI"},
        },
        {
            "title": "Gardening tips",
            "topic_tags": ["Outdoors"],
            "metadata": {"description": "Plants"},
        },
        {
            "title": "AI safety",
            "topic_tags": ["Machine Learning"],
            "metadata": {"keywords": ["AI", "Safety"]},
        },
    )
    for index, payload in enumerate(payloads):
        rid = await create_request(
            session,
            type_="url",
            status="pending",
            input_url=f"https://example{index}.com",
            normalized_url=f"https://example{index}.com",
            correlation_id=None,
            chat_id=None,
            user_id=None,
            route_version=1,
        )
        await insert_summary(
            session, request_id=rid, lang="en", json_payload=payload, is_read=False
        )

    unread_ai = await get_unread_summaries(session, limit=5, topic="AI")
    assert len(unread_ai) == 2
    assert all(
        "example0" in row["input_url"] or "example2" in row["input_url"] for row in unread_ai
    )

    unread_garden = await get_unread_summaries(session, limit=5, topic="garden")
    assert len(unread_garden) == 1
    assert "example1" in unread_garden[0]["input_url"]


async def test_get_unread_summaries_topic_filter_no_matches(
    session: AsyncSession,
) -> None:
    rid = await create_request(
        session,
        type_="url",
        status="pending",
        input_url="https://example.com",
        normalized_url="https://example.com",
        correlation_id=None,
        chat_id=None,
        user_id=None,
        route_version=1,
    )
    await insert_summary(
        session,
        request_id=rid,
        lang="en",
        json_payload={
            "title": "Quantum breakthrough",
            "topic_tags": ["Physics"],
            "metadata": {"title": "Quantum breakthrough"},
        },
        is_read=False,
    )
    unread_none = await get_unread_summaries(session, limit=5, topic="space")
    assert unread_none == []


async def test_get_unread_summaries_topic_filter_large_backlog(
    session: AsyncSession,
) -> None:
    matching_ids: list[int] = []
    for i in range(130):
        rid = await create_request(
            session,
            type_="url",
            status="pending",
            input_url=f"https://example{i}.com",
            normalized_url=f"https://example{i}.com",
            correlation_id=None,
            chat_id=None,
            user_id=None,
            route_version=1,
        )
        payload: dict[str, Any] = {
            "title": f"Article {i}",
            "topic_tags": ["general"],
            "metadata": {"title": f"Article {i}", "description": "General news"},
        }
        if i >= 120:
            payload = {
                "title": f"Gardening insights {i}",
                "topic_tags": ["gardening"],
                "metadata": {
                    "title": f"Gardening insights {i}",
                    "description": "Gardening tips",
                },
            }
            matching_ids.append(rid)

        await insert_summary(
            session, request_id=rid, lang="en", json_payload=payload, is_read=False
        )

    unread = await get_unread_summaries(session, limit=3, topic="gardening")
    assert len(unread) == 3
    assert [row["request_id"] for row in unread] == matching_ids[:3]


async def test_mark_summary_as_read(session: AsyncSession) -> None:
    rid = await create_request(
        session,
        type_="url",
        status="pending",
        correlation_id=None,
        chat_id=None,
        user_id=None,
        normalized_url="https://example.com/mark-read",
        route_version=1,
    )
    await insert_summary(
        session,
        request_id=rid,
        lang="en",
        json_payload={"title": "Test Article"},
        is_read=False,
    )
    row = await get_summary_by_request(session, rid)
    assert row is not None
    assert row["is_read"] == 0

    await mark_summary_as_read(session, rid)

    row = await get_summary_by_request(session, rid)
    assert row is not None
    assert row["is_read"] == 1


async def test_get_read_status(session: AsyncSession) -> None:
    rid1 = await create_request(
        session,
        type_="url",
        status="pending",
        correlation_id=None,
        chat_id=None,
        user_id=None,
        normalized_url="https://example.com/read-status-1",
        route_version=1,
    )
    rid2 = await create_request(
        session,
        type_="url",
        status="pending",
        correlation_id=None,
        chat_id=None,
        user_id=None,
        normalized_url="https://example.com/read-status-2",
        route_version=1,
    )
    await insert_summary(
        session,
        request_id=rid1,
        lang="en",
        json_payload={"title": "Unread Article"},
        is_read=False,
    )
    await insert_summary(
        session,
        request_id=rid2,
        lang="en",
        json_payload={"title": "Read Article"},
        is_read=True,
    )
    assert not await get_read_status(session, rid1)
    assert await get_read_status(session, rid2)
    assert not await get_read_status(session, 999)  # non-existent


async def test_get_unread_summary_by_request_id(session: AsyncSession) -> None:
    rid = await create_request(
        session,
        type_="url",
        status="pending",
        input_url="https://example.com",
        normalized_url="https://example.com",
        correlation_id=None,
        chat_id=None,
        user_id=None,
        route_version=1,
    )
    await insert_summary(
        session,
        request_id=rid,
        lang="en",
        json_payload={"title": "Test Article"},
        is_read=False,
    )

    summary = await get_unread_summary_by_request_id(session, rid)
    assert summary is not None
    assert summary["input_url"] == "https://example.com"

    await mark_summary_as_read(session, rid)
    summary = await get_unread_summary_by_request_id(session, rid)
    assert summary is None


# ---------------------------------------------------------------------------
# Bot-level commands: /unread and /read
# ---------------------------------------------------------------------------


async def test_unread_command_no_unread(database: Database) -> None:
    bot = _make_bot(database)
    msg = FakeMessage("/unread", uid=1)
    await bot._on_message(msg)
    assert len(msg._replies) == 1
    assert "No unread articles found" in msg._replies[0]


async def test_unread_command_with_unread(database: Database, session: AsyncSession) -> None:
    bot = _make_bot(database)

    details = [
        ("https://example1.com", {"title": "Article 1", "metadata": {"title": "Article 1"}}),
        ("https://example2.com", {"title": "Article 2", "metadata": {"title": "Article 2"}}),
        ("https://example3.com", {"title": "Article 3", "metadata": None}),
        (
            "https://example4.com",
            {"title": "Article 4", "metadata": '{"title": "Article 4"}'},
        ),
    ]

    for i, (url, payload) in enumerate(details, start=1):
        rid = await create_request(
            session,
            type_="url",
            status="ok",
            input_url=url,
            normalized_url=url,
            correlation_id=f"test-{i}",
            chat_id=None,
            user_id=None,
            route_version=1,
        )
        await insert_summary(
            session,
            request_id=rid,
            lang="en",
            json_payload=payload,  # type: ignore[arg-type]
            is_read=False,
        )
    await session.commit()

    msg = FakeMessage("/unread", uid=1)
    await bot._on_message(msg)

    assert len(msg._replies) == 1
    reply = msg._replies[0]
    assert "Unread Articles" in reply
    assert "Article 1" in reply
    assert "Article 2" in reply
    assert "Request ID" in reply
    assert "Article 3" in reply
    assert "Article 4" in reply


async def test_unread_command_with_topic_and_limit(
    database: Database, session: AsyncSession
) -> None:
    bot = _make_bot(database)

    details = [
        ("https://example-ai.com", "AI Revolution", ["Artificial Intelligence"]),
        ("https://example-web.com", "Web Dev", ["Web"]),
        ("https://example-ml.com", "ML Overview", ["Machine Learning"]),
    ]

    for url, title, tags in details:
        rid = await create_request(
            session,
            type_="url",
            status="ok",
            input_url=url,
            normalized_url=url,
            correlation_id="test",
            chat_id=None,
            user_id=None,
            route_version=1,
        )
        await insert_summary(
            session,
            request_id=rid,
            lang="en",
            json_payload={
                "title": title,
                "topic_tags": tags,
                "metadata": {"title": title},
            },
            is_read=False,
        )
    await session.commit()

    msg = FakeMessage("/unread ai 1", uid=1)
    await bot._on_message(msg)

    assert len(msg._replies) == 1
    reply = msg._replies[0]
    assert "topic filter: ai" in reply.casefold()
    assert "Showing up to 1 article" in reply
    assert "AI Revolution" in reply
    assert "Web Dev" not in reply


async def test_unread_command_topic_no_results(database: Database, session: AsyncSession) -> None:
    bot = _make_bot(database)
    rid = await create_request(
        session,
        type_="url",
        status="ok",
        input_url="https://example.com",
        normalized_url="https://example.com",
        correlation_id="test",
        chat_id=None,
        user_id=None,
        route_version=1,
    )
    await insert_summary(
        session,
        request_id=rid,
        lang="en",
        json_payload={
            "title": "Space Exploration",
            "topic_tags": ["Space"],
            "metadata": {"title": "Space Exploration"},
        },
        is_read=False,
    )
    await session.commit()

    msg = FakeMessage("/unread gardening", uid=1)
    await bot._on_message(msg)
    assert len(msg._replies) == 1
    assert 'No unread articles found for topic "gardening"' in msg._replies[0]


async def test_read_command_invalid_id(database: Database) -> None:
    bot = _make_bot(database)
    msg = FakeMessage("/read invalid", uid=1)
    await bot._on_message(msg)
    assert len(msg._replies) == 1
    assert "Invalid request ID" in msg._replies[0]


async def test_read_command_nonexistent_id(database: Database) -> None:
    bot = _make_bot(database)
    msg = FakeMessage("/read 999", uid=1)
    await bot._on_message(msg)
    assert len(msg._replies) == 1
    assert "not found" in msg._replies[0]


async def test_read_command_read_article(database: Database, session: AsyncSession) -> None:
    bot = _make_bot(database)
    rid = await create_request(
        session,
        type_="url",
        status="ok",
        input_url="https://example.com",
        normalized_url="https://example.com",
        correlation_id="test-read",
        chat_id=None,
        user_id=None,
        route_version=1,
    )
    await insert_summary(
        session,
        request_id=rid,
        lang="en",
        json_payload={
            "title": "Test Article",
            "summary_250": "This is a test article.",
            "metadata": {"title": "Test Article"},
        },
        is_read=False,
    )
    await session.commit()

    msg = FakeMessage(f"/read {rid}", uid=1)
    await bot._on_message(msg)
    assert len(msg._replies) >= 2
    reply_text = "\n".join(msg._replies)
    assert "Reading Article" in reply_text
    assert "Test Article" in reply_text

    assert await get_read_status(session, rid)


async def test_read_command_already_read_article(database: Database, session: AsyncSession) -> None:
    bot = _make_bot(database)
    rid = await create_request(
        session,
        type_="url",
        status="ok",
        input_url="https://example.com",
        normalized_url="https://example.com",
        correlation_id="test-read",
        chat_id=None,
        user_id=None,
        route_version=1,
    )
    await insert_summary(
        session,
        request_id=rid,
        lang="en",
        json_payload={"title": "Test Article", "metadata": {"title": "Test Article"}},
        is_read=True,
    )
    await session.commit()

    msg = FakeMessage(f"/read {rid}", uid=1)
    await bot._on_message(msg)
    assert len(msg._replies) == 1
    assert "already read" in msg._replies[0]


async def test_read_command_marks_article_read(database: Database, session: AsyncSession) -> None:
    bot = _make_bot(database)
    rid = await create_request(
        session,
        type_="url",
        status="ok",
        input_url="https://example.com",
        normalized_url="https://example.com",
        correlation_id="test-read-integration",
        chat_id=None,
        user_id=None,
        route_version=1,
    )
    await insert_summary(
        session,
        request_id=rid,
        lang="en",
        json_payload={
            "title": "Test Article",
            "tldr": "Test article summary",
            "summary_250": "This is a test article for integration testing.",
        },
        is_read=False,
    )
    await session.commit()

    msg = FakeMessage(f"/read {rid}", uid=1)
    await bot._on_message(msg)
    assert await get_read_status(session, rid)
