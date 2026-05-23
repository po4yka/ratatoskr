"""Phase 3 signal-source persistence contracts."""

from __future__ import annotations

import datetime as dt
import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, func, select

from app.config.database import DatabaseConfig
from app.core.time_utils import UTC
from app.db.models import FeedItem, Source, Subscription, Topic, User, UserSignal
from app.db.session import Database
from app.infrastructure.persistence.repositories.signal_source_repository import (
    SignalSourceRepositoryAdapter,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture
async def database() -> AsyncGenerator[Database]:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres repository tests")

    db = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    await db.migrate()
    await _clear(db)
    try:
        yield db
    finally:
        await _clear(db)
        await db.dispose()


@pytest.fixture
def repo(database: Database) -> SignalSourceRepositoryAdapter:
    return SignalSourceRepositoryAdapter(database)


async def _clear(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(delete(UserSignal))
        await session.execute(delete(Topic))
        await session.execute(delete(FeedItem))
        await session.execute(delete(Subscription))
        await session.execute(delete(Source))
        await session.execute(delete(User))


async def _user(database: Database, user_id: int, username: str) -> User:
    async with database.transaction() as session:
        user = User(telegram_user_id=user_id, username=username)
        session.add(user)
        await session.flush()
        return user


@pytest.mark.asyncio
async def test_source_subscription_item_and_signal_round_trip(
    database: Database,
    repo: SignalSourceRepositoryAdapter,
) -> None:
    await _user(database, 1001, "owner")

    source = await repo.async_upsert_source(
        kind="rss",
        external_id="https://example.com/feed.xml",
        url="https://example.com/feed.xml",
        title="Example Feed",
        metadata={"etag": "abc"},
    )
    subscription = await repo.async_subscribe(
        user_id=1001,
        source_id=source["id"],
        topic_constraints={"include": ["python"]},
    )
    item = await repo.async_upsert_feed_item(
        source_id=source["id"],
        external_id="guid-1",
        canonical_url="https://example.com/post",
        title="A useful post",
        content_text="body",
        published_at=dt.datetime(2026, 4, 30, tzinfo=UTC),
        engagement={"score": 42, "views": 100},
    )
    topic = await repo.async_upsert_topic(
        user_id=1001,
        name="Python",
        description="Python systems work",
        weight=1.5,
    )
    signal = await repo.async_record_user_signal(
        user_id=1001,
        feed_item_id=item["id"],
        topic_id=topic["id"],
        status="queued",
        heuristic_score=0.81,
        final_score=0.81,
        evidence={"matched": ["python"]},
        filter_stage="heuristic",
    )

    assert source["kind"] == "rss"
    assert subscription["source"] == source["id"]
    assert item["engagement_score"] == 42
    assert topic["name"] == "Python"
    assert signal["status"] == "queued"

    subscriptions = await repo.async_list_user_subscriptions(1001)
    assert [(row["source_kind"], row["source_title"]) for row in subscriptions] == [
        ("rss", "Example Feed")
    ]

    signals = await repo.async_list_user_signals(1001)
    assert len(signals) == 1
    assert signals[0]["feed_item_title"] == "A useful post"
    assert signals[0]["topic_name"] == "Python"


@pytest.mark.asyncio
async def test_signal_repository_scopes_reads_and_activation_to_user(
    database: Database,
    repo: SignalSourceRepositoryAdapter,
) -> None:
    await _user(database, 1001, "owner")
    await _user(database, 2002, "other")
    source = await repo.async_upsert_source(kind="telegram_channel", external_id="python_daily")
    item = await repo.async_upsert_feed_item(
        source_id=source["id"],
        external_id="42",
        title="Private post",
    )
    await repo.async_subscribe(user_id=1001, source_id=source["id"])
    await repo.async_record_user_signal(
        user_id=1001,
        feed_item_id=item["id"],
        status="candidate",
        heuristic_score=0.5,
        final_score=0.5,
        filter_stage="heuristic",
    )

    assert await repo.async_list_user_subscriptions(2002) == []
    assert await repo.async_list_user_signals(2002) == []
    assert (
        await repo.async_set_user_source_active(
            user_id=2002,
            source_id=source["id"],
            is_active=False,
        )
        is False
    )
    assert (
        await repo.async_set_user_source_active(
            user_id=1001,
            source_id=source["id"],
            is_active=False,
        )
        is True
    )
    reloaded = await repo.async_get_source(source["id"])
    assert reloaded is not None
    assert reloaded["is_active"] is False


@pytest.mark.asyncio
async def test_signal_repository_lists_unscored_candidates_once(
    database: Database,
    repo: SignalSourceRepositoryAdapter,
) -> None:
    await _user(database, 1001, "owner")
    source = await repo.async_upsert_source(kind="rss", external_id="https://example.com/feed.xml")
    await repo.async_subscribe(user_id=1001, source_id=source["id"])
    item = await repo.async_upsert_feed_item(
        source_id=source["id"],
        external_id="guid-1",
        title="Candidate",
        canonical_url="https://example.com/post",
        content_text="Candidate body",
    )

    candidates = await repo.async_list_unscored_candidates()

    assert candidates == [
        {
            "user_id": 1001,
            "source_id": source["id"],
            "source_kind": "rss",
            "feed_item_id": item["id"],
            "title": "Candidate",
            "canonical_url": "https://example.com/post",
            "content_text": "Candidate body",
            "published_at": None,
            "views": None,
            "forwards": None,
            "comments": None,
        }
    ]

    await repo.async_record_user_signal(
        user_id=1001,
        feed_item_id=item["id"],
        status="candidate",
        final_score=0.5,
    )

    assert await repo.async_list_unscored_candidates() == []


@pytest.mark.asyncio
async def test_signal_repository_records_source_backoff_and_circuit_breaker(
    repo: SignalSourceRepositoryAdapter,
) -> None:
    source = await repo.async_upsert_source(kind="rss", external_id="https://example.com/feed.xml")

    disabled = await repo.async_record_source_fetch_error(
        source_id=source["id"],
        error="timeout",
        max_errors=2,
        base_backoff_seconds=60,
    )
    after_first = await repo.async_get_source(source["id"])
    disabled_again = await repo.async_record_source_fetch_error(
        source_id=source["id"],
        error="still timeout",
        max_errors=2,
        base_backoff_seconds=60,
    )
    after_second = await repo.async_get_source(source["id"])

    assert disabled is False
    assert after_first is not None
    assert after_first["fetch_error_count"] == 1
    assert after_first["last_error"] == "timeout"
    assert disabled_again is True
    assert after_second is not None
    assert after_second["fetch_error_count"] == 2
    assert after_second["is_active"] is False

    await repo.async_record_source_fetch_success(source["id"])
    recovered = await repo.async_get_source(source["id"])
    assert recovered is not None
    assert recovered["fetch_error_count"] == 0
    assert recovered["last_error"] is None
    assert recovered["is_active"] is False


@pytest.mark.asyncio
async def test_signal_repository_updates_controls_and_manual_retry(
    database: Database,
    repo: SignalSourceRepositoryAdapter,
) -> None:
    await _user(database, 1001, "owner")
    await _user(database, 2002, "other")
    source = await repo.async_upsert_source(kind="rss", external_id="https://example.com/feed.xml")
    await repo.async_subscribe(user_id=1001, source_id=source["id"])

    updated = await repo.async_update_user_source_controls(
        user_id=1001,
        source_id=source["id"],
        is_active=False,
        fetch_interval_seconds=900,
        max_items_per_run=7,
        retry_policy={"max_errors": 3, "base_backoff_seconds": 120},
    )
    other_updated = await repo.async_update_user_source_controls(
        user_id=2002,
        source_id=source["id"],
        is_active=True,
    )
    state = await repo.async_get_source_run_state(source["id"])
    health = await repo.async_list_source_health(user_id=1001)

    assert updated is True
    assert other_updated is False
    assert state is not None
    assert state["is_active"] is False
    assert state["fetch_interval_seconds"] == 900
    assert state["max_items_per_run"] == 7
    assert state["retry_policy"] == {"max_errors": 3, "base_backoff_seconds": 120}
    assert health[0]["fetch_interval_seconds"] == 900
    assert health[0]["max_items_per_run"] == 7

    retried = await repo.async_retry_user_source(user_id=1001, source_id=source["id"])
    retry_state = await repo.async_get_source_run_state(source["id"])

    assert retried is True
    assert retry_state is not None
    assert retry_state["is_active"] is True
    assert retry_state["backoff_until"] is None


@pytest.mark.asyncio
async def test_signal_repository_upsert_preserves_source_controls(
    database: Database,
    repo: SignalSourceRepositoryAdapter,
) -> None:
    await _user(database, 1001, "owner")
    source = await repo.async_upsert_source(kind="rss", external_id="https://example.com/feed.xml")
    await repo.async_subscribe(user_id=1001, source_id=source["id"])
    await repo.async_update_user_source_controls(
        user_id=1001,
        source_id=source["id"],
        max_items_per_run=7,
        retry_policy={"max_errors": 3},
    )

    await repo.async_upsert_source(
        kind="rss",
        external_id="https://example.com/feed.xml",
        title="Updated",
        metadata={"etag": "next"},
    )
    state = await repo.async_get_source_run_state(source["id"])
    updated = await repo.async_get_source(source["id"])

    assert state is not None
    assert state["max_items_per_run"] == 7
    assert state["retry_policy"] == {"max_errors": 3}
    assert updated is not None
    assert updated["metadata_json"]["etag"] == "next"


@pytest.mark.asyncio
async def test_signal_repository_updates_feedback_and_hides_source(
    database: Database,
    repo: SignalSourceRepositoryAdapter,
) -> None:
    await _user(database, 1001, "owner")
    source = await repo.async_upsert_source(kind="rss", external_id="https://example.com/feed.xml")
    item = await repo.async_upsert_feed_item(source_id=source["id"], external_id="guid-1")
    signal = await repo.async_record_user_signal(
        user_id=1001,
        feed_item_id=item["id"],
        status="candidate",
        final_score=0.7,
    )

    assert await repo.async_update_user_signal_status(
        user_id=1001,
        signal_id=signal["id"],
        status="liked",
    )
    signals = await repo.async_list_user_signals(1001)
    assert signals[0]["status"] == "liked"

    assert await repo.async_hide_signal_source(user_id=1001, signal_id=signal["id"])
    reloaded = await repo.async_get_source(source["id"])
    assert reloaded is not None
    assert reloaded["is_active"] is False


@pytest.mark.asyncio
async def test_signal_repository_bulk_feed_items_and_subscriptions(
    database: Database,
    repo: SignalSourceRepositoryAdapter,
) -> None:
    await _user(database, 1001, "owner")
    await _user(database, 1002, "other")
    source = await repo.async_upsert_source(kind="rss", external_id="https://example.com/feed.xml")

    await repo.async_subscribe_many(
        source_id=int(source["id"]),
        user_ids=[1001, 1002, 1001],
    )
    await repo.async_subscribe_many(
        source_id=int(source["id"]),
        user_ids=[1002],
        topic_constraints={"topic": "infra"},
    )
    subscriptions = await repo.async_list_user_subscriptions(1002)
    assert len(subscriptions) == 1
    assert subscriptions[0]["is_active"] is True
    assert subscriptions[0]["topic_constraints_json"] == {"topic": "infra"}

    first_items = await repo.async_upsert_feed_items(
        source_id=int(source["id"]),
        items=[
            {
                "external_id": "guid-1",
                "canonical_url": "https://example.com/one",
                "title": "One",
                "content_text": "First",
                "published_at": dt.datetime(2026, 5, 1, tzinfo=UTC),
                "metadata": {"legacy_rss_item_id": 11},
            },
            {
                "external_id": "guid-2",
                "canonical_url": "https://example.com/two",
                "title": "Two",
                "content_text": "Second",
                "published_at": dt.datetime(2026, 5, 2, tzinfo=UTC),
                "metadata": {"legacy_rss_item_id": 12},
            },
        ],
    )
    assert [item["external_id"] for item in first_items] == ["guid-1", "guid-2"]

    updated_items = await repo.async_upsert_feed_items(
        source_id=int(source["id"]),
        items=[
            {
                "external_id": "guid-2",
                "canonical_url": "https://example.com/two-updated",
                "title": "Two updated",
                "content_text": "Updated",
                "published_at": dt.datetime(2026, 5, 3, tzinfo=UTC),
                "metadata": {"legacy_rss_item_id": 12},
            }
        ],
    )
    assert len(updated_items) == 1
    assert updated_items[0]["canonical_url"] == "https://example.com/two-updated"

    first_signals = await repo.async_record_user_signals(
        signals=[
            {
                "user_id": 1001,
                "feed_item_id": first_items[0]["id"],
                "status": "candidate",
                "heuristic_score": 0.7,
                "final_score": 0.7,
                "evidence": {"matched": ["one"]},
            }
        ]
    )
    assert len(first_signals) == 1
    assert first_signals[0]["status"] == "candidate"

    updated_signals = await repo.async_record_user_signals(
        signals=[
            {
                "user_id": 1001,
                "feed_item_id": first_items[0]["id"],
                "status": "queued",
                "heuristic_score": 0.7,
                "llm_score": 0.91,
                "final_score": 0.91,
                "evidence": {"matched": ["one"], "llm_judge": {"reason": "strong"}},
                "filter_stage": "llm_judge",
                "llm_judge": {"reason": "strong"},
                "llm_cost_usd": 0.01,
            }
        ]
    )
    assert len(updated_signals) == 1
    assert updated_signals[0]["status"] == "queued"
    assert updated_signals[0]["llm_score"] == 0.91
    assert updated_signals[0]["filter_stage"] == "llm_judge"

    async with database.session() as session:
        item_count = await session.scalar(select(func.count()).select_from(FeedItem))
        subscription_count = await session.scalar(select(func.count()).select_from(Subscription))
        signal_count = await session.scalar(select(func.count()).select_from(UserSignal))
    assert item_count == 2
    assert subscription_count == 2
    assert signal_count == 1


@pytest.mark.asyncio
async def test_signal_repository_detail_boost_and_source_health(
    database: Database,
    repo: SignalSourceRepositoryAdapter,
) -> None:
    await _user(database, 1001, "owner")
    source = await repo.async_upsert_source(
        kind="rss",
        external_id="https://example.com/feed.xml",
        url="https://example.com/feed.xml",
        title="Example Feed",
    )
    subscription = await repo.async_subscribe(user_id=1001, source_id=int(source["id"]))
    item = await repo.async_upsert_feed_item(
        source_id=source["id"],
        external_id="guid-1",
        title="Signal item",
        canonical_url="https://example.com/item",
        content_text="Useful content",
    )
    topic = await repo.async_upsert_topic(user_id=1001, name="Infra", weight=1.0)
    signal = await repo.async_record_user_signal(
        user_id=1001,
        feed_item_id=item["id"],
        topic_id=topic["id"],
        status="liked",
        final_score=0.8,
    )

    detail = await repo.async_get_user_signal(user_id=1001, signal_id=signal["id"])
    assert detail is not None
    assert detail["feed_item_id"] == item["id"]
    assert detail["feed_item_title"] == "Signal item"
    assert detail["feed_item_content_text"] == "Useful content"
    assert detail["feed_item_url"] == "https://example.com/item"

    assert await repo.async_boost_signal_topic(user_id=1001, signal_id=signal["id"], increment=0.5)
    boosted = await repo.async_get_user_signal(user_id=1001, signal_id=signal["id"])
    assert boosted is not None
    assert boosted["status"] == "boosted_topic"
    assert boosted["topic_name"] == "Infra"
    async with database.session() as session:
        topic_weight = await session.scalar(select(Topic.weight).where(Topic.id == topic["id"]))
    assert topic_weight == 1.5

    await repo.async_record_source_fetch_error(
        source_id=int(source["id"]),
        error="timeout while fetching feed",
        max_errors=10,
        base_backoff_seconds=60,
    )
    assert await repo.async_set_subscription_active(
        user_id=1001,
        subscription_id=subscription["id"],
        is_active=True,
    )
    rows = await repo.async_list_source_health(user_id=1001)
    assert len(rows) == 1
    assert rows[0]["id"] == source["id"]
    assert rows[0]["kind"] == "rss"
    assert rows[0]["title"] == "Example Feed"
    assert rows[0]["fetch_error_count"] == 1
    assert rows[0]["last_error"] == "timeout while fetching feed"
    assert rows[0]["subscription_active"] is True
    assert rows[0]["next_fetch_at"] is not None
