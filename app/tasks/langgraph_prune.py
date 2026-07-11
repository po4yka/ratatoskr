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

from dataclasses import asdict

from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves at runtime
from app.core.logging_utils import get_logger
from app.infrastructure.checkpointing.cleanup import CheckpointPruneStats, prune_expired_checkpoints
from app.infrastructure.checkpointing.runtime import _psycopg_dsn
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.tasks.broker import broker
from app.tasks.deps import get_app_config

logger = get_logger(__name__)

_PRUNE_LOCK_KEY = "task_lock:langgraph_prune"
# 10 minutes: a whole-run DELETE across 3 small checkpoint tables is fast; the
# generous TTL guards against a slow Postgres without risking a stuck lock.
_PRUNE_LOCK_TTL = 600


@broker.task(task_name="ratatoskr.langgraph.prune")
async def prune_langgraph_checkpoints(
    cfg: AppConfig = TaskiqDepends(get_app_config),
) -> CheckpointPruneStats:
    """Taskiq entrypoint: delegate to the testable runner."""
    return await _run_prune(cfg)


async def _run_prune(cfg: AppConfig) -> CheckpointPruneStats:
    """Flag-gate, acquire the Redis lock, then delegate to ``_prune_body``.

    Split out from the taskiq task so the gating + concurrency-lock behaviour is
    directly unit-testable.
    """
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
    """Delete checkpoint rows for runs whose parent request is older than retention.

    The langgraph checkpoint tables carry no timestamp and ``thread_id ==
    correlation_id`` (sacred), so run age is resolved via the parent
    ``public.requests`` row. The aged thread-id set is materialized ONCE and the
    three tables are deleted in a single transaction, so the cut is
    snapshot-consistent (the three deletes never diverge).

    Scope: this nightly job is the **age-based backstop**. The primary
    reclamation path is delete-on-terminal (lands with the graph in a later
    track). Checkpoints whose ``thread_id`` has no matching request row (true
    orphans) are intentionally left alone here — they cannot be aged from
    ``requests``, and deleting them unconditionally could race an in-flight run
    whose request row is still being written.
    """
    cp_cfg = cfg.langgraph_checkpoint
    if not cp_cfg.enabled:
        # Defence-in-depth: the scheduler only schedules this when enabled.
        logger.info("langgraph_prune_disabled")
        return CheckpointPruneStats()

    import psycopg

    schema = cp_cfg.schema_name  # validated [A-Za-z0-9_] at config time -> safe to interpolate
    dsn = _psycopg_dsn(cfg.database.dsn, cp_cfg.dsn_override)
    # OWN short-lived psycopg3 connection -- NOT app.db.session.Database (invariant 4).
    # No autocommit: the SELECT + three DELETEs share one transaction/snapshot.
    async with await psycopg.AsyncConnection.connect(dsn) as conn, conn.transaction():
        stats = await prune_expired_checkpoints(
            conn,
            schema=schema,
            retention_days=cp_cfg.retention_days,
        )

    logger.info("langgraph_prune_complete", extra=asdict(stats))
    return stats
