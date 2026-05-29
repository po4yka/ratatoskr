"""Taskiq task: periodic git mirror backup sync."""

from __future__ import annotations

from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.tasks.broker import broker
from app.tasks.deps import get_app_config, get_db

logger = get_logger(__name__)

_GIT_BACKUP_SYNC_LOCK_KEY = "task_lock:git_backup_sync"
# TTL covers the maximum expected run for a large mirror set; 1 hour default.
_GIT_BACKUP_SYNC_LOCK_TTL = 3600


@broker.task(task_name="ratatoskr.git_backup.sync")
async def sync_git_backup(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> None:
    """Mirror all due git repositories to local bare-clone storage."""
    if not cfg.git_backup.enabled:
        logger.info("git_backup_sync_disabled")
        return

    redis_client = await get_redis(cfg)
    async with RedisDistributedLock(
        redis_client, _GIT_BACKUP_SYNC_LOCK_KEY, _GIT_BACKUP_SYNC_LOCK_TTL
    ) as acquired:
        if not acquired:
            logger.info(
                "git_backup_sync_skipped_lock_held",
                extra={"key": _GIT_BACKUP_SYNC_LOCK_KEY},
            )
            return

        from app.tasks.deps import build_git_backup_task_runtime

        runtime = build_git_backup_task_runtime(cfg, db)
        summary = await runtime.service.perform_sync()
        logger.info(
            "git_backup_sync_complete",
            extra={
                "ok": summary.ok,
                "failed": summary.failed,
                "skipped": summary.skipped,
                "total": summary.total,
            },
        )
