"""Taskiq task: periodic delta-scan of ft's read-only bookmarks SQLite database.

Mirrors the simpler reconcile-style pattern (lock + delegate to a single
adapter) — no per-user fanout, no token plumbing. The
:class:`XBookmarksIngestor` itself maintains the watermark via
``MAX(synced_at)`` from ``x_bookmark_metadata``, so this task carries
no cursor state.

The ingestor opens ``bookmarks.db`` read-only via aiosqlite, never contending
with ft's own writer process. See ``app/adapters/ingestors/x_bookmarks_ingestor.py``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.tasks.broker import broker
from app.tasks.deps import build_x_bookmarks_task_runtime, get_app_config, get_db

logger = get_logger(__name__)


@dataclass
class XBookmarksSyncSummary:
    """Per-run statistics emitted by the x_bookmarks bookmark sync task."""

    bookmarks_seen: int = 0
    requests_created: int = 0
    metadata_inserted: int = 0
    metadata_updated: int = 0
    skipped_invalid_category: int = 0
    skipped_invalid_url: int = 0


_X_BOOKMARKS_SYNC_LOCK_KEY = "task_lock:x_bookmarks_sync"
# TTL covers the maximum expected run: ft typically holds < 5000 rows, the
# delta scan touches only what's new since last run, and aiosqlite reads are
# O(rows). 10 min is generous; the lock auto-releases on completion anyway.
_X_BOOKMARKS_SYNC_LOCK_TTL = 600


@broker.task(task_name="ratatoskr.x.sync_bookmarks")
async def sync_x_bookmarks(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> XBookmarksSyncSummary:
    """Run one delta-scan pass over ft's read-only bookmarks database."""
    redis_client = await get_redis(cfg)
    async with RedisDistributedLock(
        redis_client, _X_BOOKMARKS_SYNC_LOCK_KEY, _X_BOOKMARKS_SYNC_LOCK_TTL
    ) as acquired:
        if not acquired:
            logger.info(
                "x_bookmarks_sync_skipped_lock_held",
                extra={"key": _X_BOOKMARKS_SYNC_LOCK_KEY},
            )
            return XBookmarksSyncSummary()
        return await _sync_body(cfg, db)


async def _sync_body(cfg: AppConfig, db: Database) -> XBookmarksSyncSummary:
    correlation_id = f"x-bookmarks-sync-{uuid4()}"
    if not cfg.x_bookmarks.enabled:
        logger.info("x_bookmarks_sync_disabled", extra={"cid": correlation_id})
        return XBookmarksSyncSummary()

    runtime = build_x_bookmarks_task_runtime(cfg, db)
    ingestor = runtime.ingestor

    logger.info(
        "x_bookmarks_sync_starting",
        extra={
            "cid": correlation_id,
            "bookmarks_db_path": cfg.x_bookmarks.bookmarks_db_path,
        },
    )

    try:
        stats = await ingestor.sync()
    except sqlite3.OperationalError as exc:
        # Most common cause: the host-side ft mount is absent or the user has
        # not yet run `ft sync` to create the SQLite file. A single missed run
        # is preferable to a crash loop; the next scheduled tick will retry.
        logger.warning(
            "x_bookmarks_sync_db_unavailable",
            extra={
                "cid": correlation_id,
                "bookmarks_db_path": cfg.x_bookmarks.bookmarks_db_path,
                "error": str(exc),
            },
        )
        return XBookmarksSyncSummary()

    summary = XBookmarksSyncSummary(
        bookmarks_seen=stats.bookmarks_seen,
        requests_created=stats.requests_created,
        metadata_inserted=stats.metadata_inserted,
        metadata_updated=stats.metadata_updated,
        skipped_invalid_category=stats.skipped_invalid_category,
        skipped_invalid_url=stats.skipped_invalid_url,
    )
    logger.info(
        "x_bookmarks_sync_complete",
        extra={
            "cid": correlation_id,
            "bookmarks_seen": summary.bookmarks_seen,
            "requests_created": summary.requests_created,
            "metadata_inserted": summary.metadata_inserted,
            "metadata_updated": summary.metadata_updated,
            "skipped_invalid_category": summary.skipped_invalid_category,
            "skipped_invalid_url": summary.skipped_invalid_url,
        },
    )
    return summary
