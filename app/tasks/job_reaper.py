"""Taskiq task: re-drive and terminalize background jobs nothing else comes back for.

Two tables carry work that can stall permanently, for the same underlying reason:
their retry bookkeeping lives in Postgres, but the thing that would act on it runs
somewhere else, or nowhere.

``request_processing_jobs``: a failed Telegram-owned URL job is parked at
``failed`` with a ``retry_after`` backoff and attempts still on the clock, which is
exactly what a retry needs -- and then nobody comes. ``process_url_request`` is
kicked once at enqueue; taskiq's own retry middleware never fires because the task
body deliberately handles its failures rather than raising; and the API's polling
queue refuses Telegram-owned rows on purpose, since only the taskiq worker can edit
their placeholder. So the configured ``max_attempts`` was never more than one.

``import_jobs``: a bookmark import that dies without raising -- an OOM kill mid-run
-- leaves ``status='processing'`` with no lease, no TTL and no attempt counter to
notice, and the user's import shows progress that will never advance.

This sweep closes both by naming the rows and letting the owning path act: it
re-kicks the URL task (whose own ``lease_next`` still enforces the backoff and the
attempt ceiling) and terminalizes stale imports so the failure becomes visible.
"""

from __future__ import annotations

from dataclasses import dataclass

from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.tasks.broker import broker
from app.tasks.deps import get_app_config, get_db

logger = get_logger(__name__)

_JOB_REAPER_LOCK_KEY = "task_lock:job_reaper"
# Both halves are a bounded query plus a handful of enqueues; the generous TTL
# only guards against a slow Postgres. The lock heartbeat renews it anyway.
_JOB_REAPER_LOCK_TTL = 300

# Enough to drain a burst without letting one sweep flood the worker's queue --
# the next run picks up the rest a cadence later.
_MAX_REQUEUED_PER_RUN = 50


@dataclass(frozen=True)
class JobReaperStats:
    """What a single sweep did."""

    requeued_requests: int = 0
    failed_imports: int = 0


@broker.task(task_name="ratatoskr.jobs.reap")
async def reap_stalled_jobs(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> JobReaperStats:
    """Taskiq entrypoint: acquire the lock, then delegate to the testable body."""
    redis_client = await get_redis(cfg)
    async with RedisDistributedLock(
        redis_client, _JOB_REAPER_LOCK_KEY, _JOB_REAPER_LOCK_TTL
    ) as acquired:
        if not acquired:
            logger.info("job_reaper_skipped_lock_held", extra={"key": _JOB_REAPER_LOCK_KEY})
            return JobReaperStats()
        return await _reap_body(cfg, db)


async def _reap_body(cfg: AppConfig, db: Database) -> JobReaperStats:
    requeued = await _requeue_due_url_requests(db)
    failed_imports = await _fail_stale_imports(cfg, db)
    stats = JobReaperStats(requeued_requests=requeued, failed_imports=failed_imports)
    if requeued or failed_imports:
        logger.info(
            "job_reaper_swept",
            extra={"requeued_requests": requeued, "failed_imports": failed_imports},
        )
    return stats


async def _requeue_due_url_requests(db: Database) -> int:
    """Re-kick URL jobs whose retry backoff has elapsed.

    Deliberately only re-kicks: the task's own ``lease_next(by_id=...)`` re-checks
    ``retry_after`` and ``attempt_count`` under a row lock, so a duplicate kick or
    a race with a live worker resolves to a no-op there rather than to a second
    concurrent run of the same request.
    """
    from app.infrastructure.persistence.request_processing_job_repository import (
        RequestProcessingJobRepository,
    )
    from app.tasks.url_processing import process_url_request

    try:
        request_ids = await RequestProcessingJobRepository(db).list_retryable_telegram_requests(
            limit=_MAX_REQUEUED_PER_RUN
        )
    except Exception:
        logger.exception("job_reaper_url_scan_failed")
        return 0

    requeued = 0
    for request_id in request_ids:
        try:
            await process_url_request.kiq(request_id=request_id)
        except Exception:
            # One un-kickable row must not strand the rest of the batch; the next
            # sweep retries it because nothing about its DB state changed.
            logger.exception("job_reaper_requeue_failed", extra={"request_id": request_id})
            continue
        requeued += 1
        logger.info("job_reaper_requeued_request", extra={"request_id": request_id})
    return requeued


async def _fail_stale_imports(cfg: AppConfig, db: Database) -> int:
    """Mark imports abandoned mid-run as failed so the user stops waiting."""
    from app.infrastructure.persistence.repositories.import_job_repository import (
        ImportJobRepositoryAdapter,
    )

    stale_after = int(getattr(cfg.background, "stuck_processing_seconds", 900))
    try:
        job_ids = await ImportJobRepositoryAdapter(db).async_fail_stale_processing(
            older_than_seconds=stale_after
        )
    except Exception:
        logger.exception("job_reaper_import_scan_failed")
        return 0

    if job_ids:
        logger.warning(
            "job_reaper_failed_stale_imports",
            extra={"job_ids": job_ids, "stale_after_seconds": stale_after},
        )
    return len(job_ids)
