"""Scheduled topic-watch briefs over newly scored subscription signals."""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload
from taskiq import TaskiqDepends

from app.application.services.topic_change_watch import build_topic_change_brief
from app.config import AppConfig  # noqa: TC001 — taskiq resolves annotations at runtime
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.db.models import DigestDelivery, FeedItem, Topic, UserSignal
from app.db.session import Database  # noqa: TC001 — taskiq resolves annotations at runtime
from app.tasks.broker import broker
from app.tasks.deps import create_digest_bot_client, get_app_config, get_db

logger = get_logger(__name__)


@broker.task(task_name="ratatoskr.topic_change_watch.run")
async def run_topic_change_watches(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> None:
    """Send one brief per active topic when scored subscription signals changed."""
    if not getattr(cfg.signal_ingestion, "any_enabled", False):
        return
    bot = create_digest_bot_client(cfg)
    async with bot:
        async with db.session() as session:
            topics = (await session.scalars(select(Topic).where(Topic.is_active.is_(True)))).all()
        for topic in topics:
            digest_type = f"topic_change:{topic.id}"
            async with db.session() as session:
                previous = await session.scalar(
                    select(DigestDelivery)
                    .where(DigestDelivery.user_id == topic.user_id, DigestDelivery.digest_type == digest_type)
                    .order_by(DigestDelivery.delivered_at.desc())
                    .limit(1)
                )
                query = (
                    select(UserSignal)
                    .options(selectinload(UserSignal.feed_item).selectinload(FeedItem.source))
                    .where(
                        UserSignal.user_id == topic.user_id,
                        or_(UserSignal.topic_id == topic.id, UserSignal.topic_id.is_(None)),
                        UserSignal.status.in_(("candidate", "queued", "liked")),
                    )
                    .order_by(UserSignal.final_score.desc().nulls_last(), UserSignal.updated_at.asc())
                )
                if previous is not None:
                    query = query.where(UserSignal.updated_at > previous.delivered_at)
                rows = (await session.scalars(query)).all()
            signals = [
                {
                    "signal_id": row.id,
                    "feed_item_id": row.feed_item_id,
                    "source_id": row.feed_item.source_id,
                    "title": row.feed_item.title,
                    "url": row.feed_item.canonical_url,
                    "final_score": row.final_score,
                }
                for row in rows
            ]
            brief, provenance = build_topic_change_brief(
                topic_name=topic.name,
                signals=signals,
                since=previous.delivered_at if previous is not None else None,
            )
            if not brief:
                continue
            try:
                await bot.send_message(chat_id=topic.user_id, text=brief)
            except Exception:
                logger.exception("topic_change_watch_send_failed", extra={"topic_id": topic.id})
                continue
            async with db.transaction() as session:
                session.add(
                    DigestDelivery(
                        user_id=topic.user_id,
                        delivered_at=datetime.now(UTC),
                        post_count=len(provenance),
                        channel_count=len({item["source_id"] for item in provenance}),
                        digest_type=digest_type,
                        posts_json={
                            "topic_id": topic.id,
                            "source_bundle": {"signals": provenance},
                        },
                    )
                )
