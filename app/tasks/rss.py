"""Taskiq task: RSS feed polling, signal ingestion, and delivery."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.tasks.broker import broker
from app.tasks.deps import (
    build_rss_poll_task_runtime,
    get_app_config,
    get_db,
)

logger = get_logger(__name__)


@broker.task(task_name="ratatoskr.rss.poll")
async def run_rss_poll(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> None:
    """Poll RSS feeds and deliver new items to subscribers."""
    await _rss_poll_body(cfg, db)


async def _rss_poll_body(cfg: AppConfig, db: Database) -> None:
    """Core RSS poll logic — separated for direct testability."""
    from app.adapters.rss.feed_poller import poll_all_feeds

    correlation_id = f"rss_poll_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    runtime = build_rss_poll_task_runtime(cfg, db)
    logger.info("rss_poll_starting", extra={"cid": correlation_id})

    try:
        stats = await poll_all_feeds(db) if cfg.rss.enabled else {"new_item_ids": []}
        await _run_optional_source_ingestors(cfg, runtime, correlation_id)
        new_item_ids: list[int] = stats.get("new_item_ids", [])
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

        if not new_item_ids or not cfg.rss.auto_summarize:
            return

        delivery_service = runtime.create_delivery_service()
        bot = runtime.create_bot_client()

        async with bot:

            async def send_message(user_id: int, text: str) -> None:
                await bot.send_message(chat_id=user_id, text=text)

            delivery_stats = await delivery_service.deliver_new_items(
                send_message,
                new_item_ids=new_item_ids,
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
