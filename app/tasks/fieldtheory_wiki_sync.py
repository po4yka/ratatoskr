"""Taskiq task: periodic delta-scan of the fieldtheory wiki library directory.

Mirrors the reconcile-style pattern of ``app/tasks/fieldtheory_sync.py`` (the
bookmark sync sibling) and ``app/tasks/reconcile_vector_index.py``: a thin
outer task handles the Redis distributed lock + short-circuit, and an inner
``_wiki_sync_body`` handles the ``cfg.fieldtheory.enabled`` gate and delegates
to :class:`FieldTheoryWikiSyncService`. The service walks
``cfg.fieldtheory.library_path``, content-hashes each ``*.md`` page, embeds
changed pages into the shared Qdrant collection as
``entity_type="fieldtheory_wiki"`` points, and hard-deletes orphan paths.

The wiki has no Postgres mirror; Qdrant is its sole persistence beyond the
source filesystem (see ``docs/explanation/fieldtheory-integration.md``).
"""

from __future__ import annotations

from taskiq import TaskiqDepends

from app.application.services.fieldtheory_wiki_sync import WikiSyncSummary
from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.tasks.broker import broker
from app.tasks.deps import (
    build_fieldtheory_wiki_sync_task_runtime,
    get_app_config,
    get_db,
)

logger = get_logger(__name__)

_FIELDTHEORY_WIKI_SYNC_LOCK_KEY = "task_lock:fieldtheory_wiki_sync"
# Wiki walks are I/O bound on the embedding model: a few hundred pages * a
# few seconds per embedding stays comfortably under 30 minutes. The lock
# auto-releases on completion; the TTL only matters when a worker crashes
# mid-run.
_FIELDTHEORY_WIKI_SYNC_LOCK_TTL = 1800


@broker.task(task_name="ratatoskr.fieldtheory.sync_wiki")
async def sync_fieldtheory_wiki(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> WikiSyncSummary:
    """Run one delta-scan pass over the fieldtheory wiki library directory."""
    redis_client = await get_redis(cfg)
    async with RedisDistributedLock(
        redis_client,
        _FIELDTHEORY_WIKI_SYNC_LOCK_KEY,
        _FIELDTHEORY_WIKI_SYNC_LOCK_TTL,
    ) as acquired:
        if not acquired:
            logger.info(
                "fieldtheory_wiki_sync_skipped_lock_held",
                extra={"key": _FIELDTHEORY_WIKI_SYNC_LOCK_KEY},
            )
            return WikiSyncSummary()
        return await _wiki_sync_body(cfg, db)


async def _wiki_sync_body(cfg: AppConfig, db: Database) -> WikiSyncSummary:
    if not cfg.fieldtheory.enabled:
        logger.info(
            "fieldtheory_wiki_sync_disabled",
            extra={"library_path": cfg.fieldtheory.library_path},
        )
        return WikiSyncSummary()

    runtime = build_fieldtheory_wiki_sync_task_runtime(cfg, db)
    logger.info(
        "fieldtheory_wiki_sync_starting",
        extra={"library_path": cfg.fieldtheory.library_path},
    )
    summary: WikiSyncSummary = await runtime.service.sync()
    logger.info(
        "fieldtheory_wiki_sync_complete",
        extra={
            "files_seen": summary.files_seen,
            "files_changed": summary.files_changed,
            "files_skipped": summary.files_skipped,
            "orphans_deleted": summary.orphans_deleted,
        },
    )
    return summary
