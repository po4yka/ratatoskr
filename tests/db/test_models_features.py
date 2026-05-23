from __future__ import annotations

import os
from typing import cast

import pytest
from sqlalchemy import Table, select, text

from app.config.database import DatabaseConfig
from app.db.base import Base
from app.db.models import (
    ALL_MODELS,
    AggregationSession,
    AggregationSessionItem,
    AutomationRule,
    BatchSession,
    BatchSessionItem,
    Channel,
    ChannelCategory,
    ChannelPost,
    ChannelPostAnalysis,
    ChannelSubscription,
    Collection,
    CollectionCollaborator,
    CollectionInvite,
    CollectionItem,
    CustomDigest,
    DigestDelivery,
    FeedItem,
    ImportJob,
    LLMCall,
    Request,
    RSSFeed,
    RSSFeedItem,
    RSSFeedSubscription,
    RSSItemDelivery,
    RuleExecutionLog,
    Source,
    Subscription,
    Summary,
    SummaryFeedback,
    SummaryHighlight,
    SummaryTag,
    Tag,
    Topic,
    User,
    UserBackup,
    UserDigestPreference,
    UserGoal,
    UserSignal,
    WebhookDelivery,
    WebhookSubscription,
)
from app.db.session import Database
from app.db.types import _utcnow


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


def _all_tables() -> list[Table]:
    return [cast("Table", model.__table__) for model in ALL_MODELS]


@pytest.mark.asyncio
async def test_feature_models_round_trip_against_postgres() -> None:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres model smoke test")

    database = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    now = _utcnow()
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all, tables=list(reversed(_all_tables())))
            await connection.run_sync(Base.metadata.create_all, tables=_all_tables())

        async with database.transaction() as session:
            user = User(telegram_user_id=501, username="feature")
            request = Request(type="url", status="done", dedupe_hash="feature-dedupe")
            session.add_all([user, request])
            await session.flush()

            llm_call = LLMCall(request_id=request.id, provider="openrouter")
            summary = Summary(request_id=request.id, lang="en", json_payload={"ok": True})
            session.add_all([llm_call, summary])
            await session.flush()

            aggregation = AggregationSession(
                user_id=user.telegram_user_id,
                correlation_id="agg-feature",
                total_items=1,
                bundle_metadata_json={"kind": "mixed"},
            )
            batch = BatchSession(
                user_id=user.telegram_user_id,
                correlation_id="batch-feature",
                total_urls=1,
            )
            collection = Collection(user_id=user.telegram_user_id, name="Read later")
            channel = Channel(username="feature_channel", title="Feature Channel")
            category = ChannelCategory(user_id=user.telegram_user_id, name="Tech")
            rss_feed = RSSFeed(url="https://example.com/feed.xml", title="Feed")
            webhook = WebhookSubscription(
                user_id=user.telegram_user_id,
                url="https://example.com/hook",
                secret="secret",
                events_json=["summary.created"],
            )
            rule = AutomationRule(
                user_id=user.telegram_user_id,
                name="Rule",
                event_type="summary.created",
                conditions_json=[],
                actions_json=[],
            )
            import_job = ImportJob(user_id=user.telegram_user_id, source_format="json")
            backup = UserBackup(user_id=user.telegram_user_id)
            topic = Topic(user_id=user.telegram_user_id, name="AI")
            tag = Tag(user_id=user.telegram_user_id, name="AI", normalized_name="ai")
            session.add_all(
                [
                    aggregation,
                    batch,
                    collection,
                    channel,
                    category,
                    rss_feed,
                    webhook,
                    rule,
                    import_job,
                    backup,
                    topic,
                    tag,
                ]
            )
            await session.flush()

            channel_subscription = ChannelSubscription(
                user_id=user.telegram_user_id,
                channel_id=channel.id,
                category_id=category.id,
            )
            channel_post = ChannelPost(
                channel_id=channel.id,
                message_id=1,
                text="Post",
                date=now,
            )
            rss_subscription = RSSFeedSubscription(
                user_id=user.telegram_user_id,
                feed_id=rss_feed.id,
                category_id=category.id,
            )
            rss_item = RSSFeedItem(feed_id=rss_feed.id, guid="guid-1")
            source = Source(kind="rss", external_id="feed-1", legacy_rss_feed_id=rss_feed.id)
            collection_item = CollectionItem(collection_id=collection.id, summary_id=summary.id)
            collaborator = CollectionCollaborator(
                collection_id=collection.id,
                user_id=user.telegram_user_id,
                role="owner",
            )
            invite = CollectionInvite(
                collection_id=collection.id, token="invite-token", role="viewer"
            )
            feedback = SummaryFeedback(
                user_id=user.telegram_user_id,
                summary_id=summary.id,
                rating=1,
            )
            digest = CustomDigest(user_id=user.telegram_user_id, summary_ids=str(summary.id))
            highlight = SummaryHighlight(
                user_id=user.telegram_user_id,
                summary_id=summary.id,
                text="highlight",
            )
            goal = UserGoal(user_id=user.telegram_user_id, goal_type="daily", target_count=3)
            summary_tag = SummaryTag(summary_id=summary.id, tag_id=tag.id)
            delivery = DigestDelivery(
                user_id=user.telegram_user_id,
                digest_type="daily",
                posts_json=[1],
            )
            preference = UserDigestPreference(user_id=user.telegram_user_id, timezone="UTC")
            webhook_delivery = WebhookDelivery(
                subscription_id=webhook.id,
                event_type="summary.created",
                payload_json={"summary_id": summary.id},
                success=True,
            )
            rule_log = RuleExecutionLog(
                rule_id=rule.id,
                summary_id=summary.id,
                event_type="summary.created",
                matched=True,
            )
            session.add_all(
                [
                    channel_subscription,
                    channel_post,
                    rss_subscription,
                    rss_item,
                    source,
                    collection_item,
                    collaborator,
                    invite,
                    feedback,
                    digest,
                    highlight,
                    goal,
                    summary_tag,
                    delivery,
                    preference,
                    webhook_delivery,
                    rule_log,
                ]
            )
            await session.flush()

            channel_analysis = ChannelPostAnalysis(
                post_id=channel_post.id,
                real_topic="AI",
                tldr="Short",
                llm_call_id=llm_call.id,
            )
            batch_item = BatchSessionItem(
                batch_session_id=batch.id,
                request_id=request.id,
                position=0,
            )
            aggregation_item = AggregationSessionItem(
                aggregation_session_id=aggregation.id,
                request_id=request.id,
                position=0,
                source_kind="url",
                source_item_id="item-1",
                source_dedupe_key="dedupe-1",
            )
            subscription = Subscription(
                user_id=user.telegram_user_id,
                source_id=source.id,
                legacy_rss_subscription_id=rss_subscription.id,
            )
            feed_item = FeedItem(
                source_id=source.id,
                external_id="feed-item-1",
                legacy_rss_item_id=rss_item.id,
            )
            session.add_all(
                [channel_analysis, batch_item, aggregation_item, subscription, feed_item]
            )
            await session.flush()

            signal = UserSignal(
                user_id=user.telegram_user_id,
                feed_item_id=feed_item.id,
                topic_id=topic.id,
                status="candidate",
            )
            item_delivery = RSSItemDelivery(user_id=user.telegram_user_id, item_id=rss_item.id)
            session.add_all([signal, item_delivery])

        async with database.session() as session:
            stored_source = await session.scalar(
                select(Source).where(Source.external_id == "feed-1")
            )
            stored_webhook = await session.scalar(select(WebhookSubscription))
            stored_tag = await session.scalar(select(Tag).where(Tag.normalized_name == "ai"))

        assert stored_source is not None
        assert stored_source.metadata_json is None
        assert stored_webhook is not None
        assert stored_webhook.events_json == ["summary.created"]
        assert stored_tag is not None
        assert stored_tag.name == "AI"
        assert len(ALL_MODELS) == 63
    finally:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all, tables=list(reversed(_all_tables())))
            await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await database.dispose()
