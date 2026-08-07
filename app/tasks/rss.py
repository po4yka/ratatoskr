"""Taskiq task: RSS feed polling, signal ingestion, and delivery."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.tasks.broker import broker
from app.tasks.deps import (
    build_rss_poll_task_runtime,
    get_app_config,
    get_db,
)

logger = get_logger(__name__)

_RSS_POLL_LOCK_KEY = "task_lock:rss_poll"
# Base TTL; RedisDistributedLock renews it via a background heartbeat (~ttl/3)
# while the run is in progress, so a poll that outlives this keeps its lock
# rather than losing it to the next scheduled run.
_RSS_POLL_LOCK_TTL = 1800


@broker.task(task_name="ratatoskr.rss.poll", retry_on_error=True, max_retries=3)
async def run_rss_poll(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> None:
    """Poll RSS feeds and deliver new items to subscribers.

    Serialized like every other scheduled task, and for a sharper reason than
    most: delivery sends each message to Telegram *before* marking the
    (user, item) pair delivered, and the undelivered query takes no row claim.
    Two overlapping runs therefore read the same ledger and send the same digest
    twice. Overlap was reachable both ways -- a poll of up to
    ``max_feeds_per_poll`` feeds with inline summarization can outlast the
    interval, and this task re-raises, so taskiq's retry middleware re-kicks it
    immediately while the original may still be winding down.
    """
    redis_client = await get_redis(cfg)
    async with RedisDistributedLock(
        redis_client, _RSS_POLL_LOCK_KEY, _RSS_POLL_LOCK_TTL
    ) as acquired:
        if not acquired:
            logger.info("rss_poll_skipped_lock_held", extra={"key": _RSS_POLL_LOCK_KEY})
            return
        await _rss_poll_body(cfg, db)


async def _rss_poll_body(cfg: AppConfig, db: Database) -> None:
    """Core RSS poll logic — separated for direct testability."""
    from app.adapters.rss.feed_poller import poll_all_feeds

    correlation_id = f"rss_poll_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    runtime = build_rss_poll_task_runtime(cfg, db)
    logger.info("rss_poll_starting", extra={"cid": correlation_id})

    try:
        stats = (
            await poll_all_feeds(
                db,
                limit=cfg.rss.max_feeds_per_poll,
                concurrency=cfg.rss.poll_concurrency,
            )
            if cfg.rss.enabled
            else {"new_item_ids": []}
        )
        await _run_optional_source_ingestors(cfg, runtime, correlation_id)
        logger.info(
            "rss_poll_fetched",
            extra={
                "cid": correlation_id,
                "polled": stats.get("polled", 0),
                "new_items": stats.get("new_items", 0),
                "errors": stats.get("errors", 0),
            },
        )

        await _run_signal_ingestion(cfg, runtime, correlation_id)

        if not cfg.rss.enabled or not cfg.rss.auto_summarize:
            return

        delivery_service = runtime.create_delivery_service()
        bot = runtime.create_bot_client()

        async with bot:

            async def send_message(user_id: int, text: str) -> None:
                await bot.send_message(chat_id=user_id, text=text)

            delivery_stats = await delivery_service.deliver_new_items(
                send_message,
                # Query the complete undelivered ledger, not only rows inserted by
                # this poll. A prior send failure therefore remains retryable even
                # when the next fetch returns the item as a duplicate.
                new_item_ids=None,
            )
            logger.info(
                "rss_poll_delivery_complete",
                extra={"cid": correlation_id, **delivery_stats},
            )

    except Exception as exc:
        logger.exception(
            "rss_poll_failed",
            extra={"cid": correlation_id, "error": str(exc)},
        )
        raise


async def _run_signal_ingestion(cfg: AppConfig, runtime: Any, correlation_id: str) -> None:
    signal_sources_enabled = bool(getattr(cfg.signal_ingestion, "any_enabled", False))
    if not signal_sources_enabled:
        logger.info("signal_ingestion_skipped", extra={"cid": correlation_id})
        return
    try:
        worker = runtime.create_signal_ingestion_worker()
        limit = getattr(cfg.rss, "max_items_per_poll", 100)
        stats = await worker.run_once(limit=limit)
        logger.info("signal_ingestion_complete", extra={"cid": correlation_id, **stats})
    except Exception as exc:
        logger.exception(
            "signal_ingestion_failed",
            extra={"cid": correlation_id, "error": str(exc)},
        )


async def _run_optional_source_ingestors(cfg: AppConfig, runtime: Any, correlation_id: str) -> None:
    if not cfg.signal_ingestion.any_enabled:
        logger.info("source_ingestion_skipped", extra={"cid": correlation_id})
        return
    try:
        runner = runtime.create_source_ingestion_runner()
        stats = await runner.run_once()
        logger.info("source_ingestion_complete", extra={"cid": correlation_id, **stats})
    except Exception as exc:
        logger.exception(
            "source_ingestion_failed",
            extra={"cid": correlation_id, "error": str(exc)},
        )
