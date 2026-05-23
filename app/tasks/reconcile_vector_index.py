"""Taskiq task: steady-state vector-index reconciler.

Periodically scans ``summary_embeddings`` for rows whose ``last_indexed_at``
lags ``summaries.updated_at`` (or is unset entirely) and re-runs
:class:`SummaryEmbeddingGenerator` against each summary. Acts as a fallback
for the CocoIndex live updater.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.db.models import Request, Summary, SummaryEmbedding
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.tasks.broker import broker
from app.tasks.deps import build_vector_reconcile_task_runtime, get_app_config, get_db

logger = get_logger(__name__)


@dataclass
class ReconcileSummary:
    """Per-run statistics emitted by the reconciler."""

    scanned: int
    requeued: int
    skipped: int
    failed: int


_VECTOR_RECONCILE_LOCK_KEY = "task_lock:vector_reconcile"
# TTL covers the maximum expected run: 100-row batch * ~3 s/embedding ≈ 5 min.
_VECTOR_RECONCILE_LOCK_TTL = 300


@broker.task(task_name="ratatoskr.vector.reconcile")
async def reconcile_vector_index(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> ReconcileSummary:
    """Re-embed summaries whose embedding row is stale relative to the source."""
    redis_client = await get_redis(cfg)
    async with RedisDistributedLock(
        redis_client, _VECTOR_RECONCILE_LOCK_KEY, _VECTOR_RECONCILE_LOCK_TTL
    ) as acquired:
        if not acquired:
            logger.info(
                "vector_reconcile_skipped_lock_held",
                extra={"key": _VECTOR_RECONCILE_LOCK_KEY},
            )
            return ReconcileSummary(scanned=0, requeued=0, skipped=0, failed=0)
        return await _reconcile_body(cfg, db)


async def _reconcile_body(cfg: AppConfig, db: Database) -> ReconcileSummary:
    correlation_id = f"vector-reconcile-{uuid4()}"
    if not cfg.vector_reconcile.enabled:
        logger.info("vector_reconcile_disabled", extra={"cid": correlation_id})
        return ReconcileSummary(scanned=0, requeued=0, skipped=0, failed=0)

    batch_size = cfg.vector_reconcile.batch_size
    rows = await _fetch_stale_summaries(db, limit=batch_size)
    if not rows:
        logger.info(
            "vector_reconcile_nothing_to_do",
            extra={"cid": correlation_id, "batch_size": batch_size},
        )
        return ReconcileSummary(scanned=0, requeued=0, skipped=0, failed=0)

    generator = _build_generator(cfg, db)

    requeued = 0
    skipped = 0
    failed = 0
    for row in rows:
        summary_id: int = row["summary_id"]
        payload = row["json_payload"]
        if not isinstance(payload, dict):
            skipped += 1
            continue
        try:
            ok = await generator.generate_embedding_for_summary(
                summary_id=summary_id,
                payload=payload,
                language=row.get("lang_detected"),
                force=True,
            )
        except Exception:
            logger.exception(
                "vector_reconcile_summary_failed",
                extra={"cid": correlation_id, "summary_id": summary_id},
            )
            failed += 1
            continue
        if ok:
            requeued += 1
        else:
            skipped += 1

    summary = ReconcileSummary(
        scanned=len(rows),
        requeued=requeued,
        skipped=skipped,
        failed=failed,
    )
    logger.info(
        "vector_reconcile_complete",
        extra={
            "cid": correlation_id,
            "scanned": summary.scanned,
            "requeued": summary.requeued,
            "skipped": summary.skipped,
            "failed": summary.failed,
        },
    )
    return summary


async def _fetch_stale_summaries(db: Database, *, limit: int) -> list[dict[str, Any]]:
    """Return summaries whose embedding row is missing or older than the source.

    A row is "stale" when:
      * no ``summary_embeddings`` row exists, OR
      * ``last_indexed_at`` is NULL (legacy data predating reconciler wiring), OR
      * ``last_indexed_at`` is older than ``summaries.updated_at``.

    Soft-deleted summaries are excluded.
    """
    if limit <= 0:
        return []
    async with db.session() as session:
        stmt = (
            select(
                Summary.id.label("summary_id"),
                Summary.json_payload,
                Request.lang_detected,
            )
            .join(Request, Summary.request_id == Request.id)
            .outerjoin(SummaryEmbedding, SummaryEmbedding.summary_id == Summary.id)
            .where(
                Summary.is_deleted.is_(False),
                Summary.json_payload.is_not(None),
                (
                    (SummaryEmbedding.id.is_(None))
                    | (SummaryEmbedding.last_indexed_at.is_(None))
                    | (SummaryEmbedding.last_indexed_at < Summary.updated_at)
                ),
            )
            .order_by(Summary.updated_at.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return [dict(row._mapping) for row in result]


def _build_generator(cfg: AppConfig, db: Database) -> Any:
    """Construct a generator wired against the application repositories."""
    return build_vector_reconcile_task_runtime(cfg, db).embedding_generator
