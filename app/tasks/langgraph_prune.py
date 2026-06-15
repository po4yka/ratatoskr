"""Taskiq task: nightly LangGraph checkpoint prune (ADR-0004).

Drops checkpoint rows for runs older than the retention window. The langgraph
checkpoint tables carry no timestamp column and `thread_id == correlation_id`
(sacred, ADR-0011), so a "run older than N days" is resolved via the parent
``public.requests`` row's ``created_at``. Deleting by ``thread_id`` removes the
whole run's state across all three checkpoint tables — exactly ADR-0004's
"drop a run's checkpoints" backstop (the primary path is delete-on-terminal,
landed with the graph in a later track).

Invariant 4 (ADR-0018): this task opens its OWN short-lived psycopg3 connection
and must NOT route through ``app.db.session.Database``. ``psycopg`` is imported
lazily inside the body so the module stays importable on the default worker
image, which does not install the optional ``graph`` extra.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves at runtime
from app.core.logging_utils import get_logger
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.tasks.broker import broker
from app.tasks.deps import get_app_config

logger = get_logger(__name__)

_PRUNE_LOCK_KEY = "task_lock:langgraph_prune"
# 10 minutes: a whole-run DELETE across 3 small checkpoint tables is fast; the
# generous TTL guards against a slow Postgres without risking a stuck lock.
_PRUNE_LOCK_TTL = 600


@dataclass
class CheckpointPruneStats:
    """Rows deleted from each checkpoint table."""

    checkpoints: int = 0
    checkpoint_blobs: int = 0
    checkpoint_writes: int = 0


@broker.task(task_name="ratatoskr.langgraph.prune")
async def prune_langgraph_checkpoints(
    cfg: AppConfig = TaskiqDepends(get_app_config),
) -> CheckpointPruneStats:
    """Acquire the Redis lock and delegate to ``_prune_body`` (early-return if off)."""
    cp_cfg = cfg.langgraph_checkpoint
    if not cp_cfg.enabled:
        logger.info("langgraph_prune_disabled")
        return CheckpointPruneStats()

    redis_client = await get_redis(cfg)
    async with RedisDistributedLock(redis_client, _PRUNE_LOCK_KEY, _PRUNE_LOCK_TTL) as acquired:
        if not acquired:
            logger.info("langgraph_prune_skipped_lock_held", extra={"key": _PRUNE_LOCK_KEY})
            return CheckpointPruneStats()
        return await _prune_body(cfg)


async def _prune_body(cfg: AppConfig) -> CheckpointPruneStats:
    """Delete checkpoint rows for runs whose request is older than retention."""
    cp_cfg = cfg.langgraph_checkpoint
    if not cp_cfg.enabled:
        # Defence-in-depth: the scheduler only schedules this when enabled, but
        # guard here too so the body never opens a connection when off.
        logger.info("langgraph_prune_disabled")
        return CheckpointPruneStats()

    import psycopg

    schema = cp_cfg.schema_name
    # psycopg3 DSN: strip the SQLAlchemy '+asyncpg' driver suffix.
    dsn = (cp_cfg.dsn_override or cfg.database.dsn).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=cp_cfg.retention_days)

    # thread_id == correlation_id; resolve run age via the parent request row.
    thread_subquery = (
        "SELECT correlation_id FROM public.requests "
        "WHERE correlation_id IS NOT NULL AND created_at < %(cutoff)s"
    )

    stats = CheckpointPruneStats()
    # OWN short-lived psycopg3 connection -- NOT app.db.session.Database (invariant 4).
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            cur = await conn.execute(
                f'DELETE FROM "{schema}".{table} WHERE thread_id IN ({thread_subquery})',
                {"cutoff": cutoff},
            )
            setattr(stats, table, cur.rowcount or 0)

    logger.info("langgraph_prune_complete", extra=asdict(stats))
    return stats
