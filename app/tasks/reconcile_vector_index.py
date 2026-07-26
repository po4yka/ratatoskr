"""Taskiq task: steady-state vector-index reconciler.

Periodically scans ``summary_embeddings`` for rows whose ``last_indexed_at``
lags ``summaries.updated_at`` (or is unset entirely) and re-runs
:class:`SummaryEmbeddingGenerator` against each summary. This is the
convergence/backfill path complementing the synchronous fast-path writer in
the summarize graph's persist node.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from taskiq import TaskiqDepends

from app.adapters.content.streaming.operation_streams import (
    publish_operation_event,
    vector_reconcile_topic,
)
from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.db.models import Request, Summary, SummaryEmbedding
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.infrastructure.vector.point_ids import summary_point_id
from app.infrastructure.vector.summary_point import (
    build_summary_qdrant_payload,
    coerce_summary_payload,
    extract_indexable_text,
)
from app.observability.metrics import (
    compute_vector_reconcile_oldest_lag_seconds,
    record_vector_reconcile_rows,
    record_vector_reconcile_run,
    set_vector_reconcile_oldest_lag_seconds,
)
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


@broker.task(task_name="ratatoskr.vector.reconcile", retry_on_error=True, max_retries=3)
async def reconcile_vector_index(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> ReconcileSummary:
    """Re-embed summaries whose embedding row is stale relative to the source."""
    correlation_id = f"vector-reconcile-{uuid4()}"
    try:
        redis_client = await get_redis(cfg)
        async with RedisDistributedLock(
            redis_client, _VECTOR_RECONCILE_LOCK_KEY, _VECTOR_RECONCILE_LOCK_TTL
        ) as acquired:
            if not acquired:
                logger.info(
                    "vector_reconcile_skipped_lock_held",
                    extra={"key": _VECTOR_RECONCILE_LOCK_KEY},
                )
                summary = ReconcileSummary(scanned=0, requeued=0, skipped=0, failed=0)
                _record_reconcile_metrics(summary, oldest_lag_seconds=0.0, run_status="success")
                _publish_vector_reconcile_event(
                    correlation_id,
                    "done",
                    {**_vector_terminal_payload(summary), "status": "lock_held"},
                )
                return summary
            return await _reconcile_body(cfg, db, correlation_id=correlation_id)
    except Exception as exc:
        _publish_vector_reconcile_event(
            correlation_id,
            "error",
            {"phase": "failed", "message": str(exc)},
        )
        record_vector_reconcile_run(status="error")
        raise


async def _reconcile_body(
    cfg: AppConfig,
    db: Database,
    *,
    correlation_id: str | None = None,
) -> ReconcileSummary:
    correlation_id = correlation_id or f"vector-reconcile-{uuid4()}"
    _publish_vector_reconcile_event(
        correlation_id,
        "phase",
        {"phase": "starting", "batch_size": cfg.vector_reconcile.batch_size},
    )
    if not cfg.vector_reconcile.enabled:
        logger.info("vector_reconcile_disabled", extra={"cid": correlation_id})
        summary = ReconcileSummary(scanned=0, requeued=0, skipped=0, failed=0)
        _record_reconcile_metrics(summary, oldest_lag_seconds=0.0, run_status="success")
        _publish_vector_reconcile_event(correlation_id, "done", _vector_terminal_payload(summary))
        return summary

    batch_size = cfg.vector_reconcile.batch_size
    _publish_vector_reconcile_event(correlation_id, "phase", {"phase": "scanning"})
    rows = await _fetch_stale_summaries(db, limit=batch_size)
    oldest_lag_seconds = compute_vector_reconcile_oldest_lag_seconds(rows)
    _publish_vector_reconcile_event(
        correlation_id,
        "rows_scanned",
        {"rows_scanned": len(rows), "oldest_lag_seconds": oldest_lag_seconds},
    )
    if not rows:
        logger.info(
            "vector_reconcile_nothing_to_do",
            extra={"cid": correlation_id, "batch_size": batch_size},
        )
        summary = ReconcileSummary(scanned=0, requeued=0, skipped=0, failed=0)
        _record_reconcile_metrics(
            summary,
            oldest_lag_seconds=oldest_lag_seconds,
            run_status="success",
        )
        _publish_vector_reconcile_event(correlation_id, "done", _vector_terminal_payload(summary))
        return summary

    _publish_vector_reconcile_event(
        correlation_id,
        "phase",
        {"phase": "embedding", "rows": len(rows)},
    )
    runtime = _build_runtime(cfg, db)
    generator = runtime.embedding_generator

    # Batch-encode all stale rows with one native encode() per language, instead
    # of one model.encode() per row (5-10x slower on MiniLM). force=True because
    # _fetch_stale_summaries already selected only rows that need re-indexing.
    batch = await generator.generate_embeddings_for_summaries(
        [(row["summary_id"], row["json_payload"], row.get("lang_detected")) for row in rows],
        force=True,
    )
    indexed_vectors = await _sync_summary_vectors(cfg, runtime, rows)
    _publish_vector_reconcile_event(
        correlation_id,
        "rows_requeued",
        {"rows_requeued": indexed_vectors},
    )

    summary = ReconcileSummary(
        scanned=len(rows),
        requeued=indexed_vectors,
        skipped=batch.skipped,
        failed=batch.failed,
    )
    logger.info(
        "vector_reconcile_complete",
        extra={
            "cid": correlation_id,
            "scanned": summary.scanned,
            "requeued": summary.requeued,
            "skipped": summary.skipped,
            "failed": summary.failed,
            "embedding_rows": batch.indexed,
            "oldest_lag_seconds": oldest_lag_seconds,
        },
    )
    _record_reconcile_metrics(
        summary,
        oldest_lag_seconds=oldest_lag_seconds,
        run_status="success",
    )
    _publish_vector_reconcile_event(correlation_id, "done", _vector_terminal_payload(summary))
    return summary


def _publish_vector_reconcile_event(
    correlation_id: str,
    kind: str,
    payload: dict[str, Any],
) -> None:
    publish_operation_event(
        topic=vector_reconcile_topic(correlation_id),
        kind=kind,
        correlation_id=correlation_id,
        payload=payload,
    )


def _vector_terminal_payload(summary: ReconcileSummary) -> dict[str, int]:
    return {
        "scanned": summary.scanned,
        "requeued": summary.requeued,
        "skipped": summary.skipped,
        "failed": summary.failed,
    }


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
                Summary.request_id,
                Summary.json_payload,
                Summary.lang,
                Summary.updated_at,
                Request.lang_detected,
                SummaryEmbedding.last_indexed_at,
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


def _record_reconcile_metrics(
    summary: ReconcileSummary,
    *,
    oldest_lag_seconds: float,
    run_status: str,
) -> None:
    record_vector_reconcile_rows(
        scanned=summary.scanned,
        requeued=summary.requeued,
        skipped=summary.skipped,
        failed=summary.failed,
    )
    set_vector_reconcile_oldest_lag_seconds(oldest_lag_seconds)
    record_vector_reconcile_run(status=run_status)


def _build_runtime(cfg: AppConfig, db: Database) -> Any:
    """Construct a runtime wired against the application repositories."""
    return build_vector_reconcile_task_runtime(cfg, db)


async def _sync_summary_vectors(
    cfg: AppConfig,
    runtime: Any,
    rows: list[dict[str, Any]],
) -> int:
    """Write regenerated summary embeddings to Qdrant and mark successful rows indexed."""
    vector_store = runtime.vector_store
    if vector_store is None or not getattr(vector_store, "available", False):
        logger.info("vector_reconcile_qdrant_unavailable", extra={"rows": len(rows)})
        return 0

    summary_ids = [
        row["summary_id"]
        for row in rows
        if isinstance(row.get("summary_id"), int) and isinstance(row.get("request_id"), int)
    ]
    embeddings = await runtime.embedding_repository.async_get_summary_embeddings(summary_ids)
    embeddings_by_summary_id = {
        embedding["summary_id"]: embedding
        for embedding in embeddings
        if isinstance(embedding.get("summary_id"), int)
    }
    indexed_content_hashes: dict[int, str] = {}
    request_ids: list[int] = []
    raw_ids: list[str] = []
    vectors: list[list[float]] = []
    point_payloads: list[dict[str, Any]] = []
    embedding_service = runtime.embedding_generator.embedding_service

    for row in rows:
        summary_id = row.get("summary_id")
        request_id = row.get("request_id")
        if not isinstance(summary_id, int) or not isinstance(request_id, int):
            continue
        embedding_row = embeddings_by_summary_id.get(summary_id)
        if not embedding_row:
            continue
        payload, raw_fallback = coerce_summary_payload(row.get("json_payload"))
        if not payload and not raw_fallback:
            continue
        text = extract_indexable_text(payload, raw_fallback=raw_fallback)
        current_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if embedding_row.get("content_hash") != current_hash:
            logger.warning(
                "vector_reconcile_embedding_hash_mismatch",
                extra={"summary_id": summary_id, "request_id": request_id},
            )
            continue
        vector = embedding_service.deserialize_embedding(embedding_row["embedding_blob"])
        vector_list = vector.tolist() if hasattr(vector, "tolist") else list(vector)
        lang = row.get("lang") or row.get("lang_detected")
        point_payload = build_summary_qdrant_payload(
            summary_id,
            request_id,
            lang if isinstance(lang, str) else None,
            payload,
            cfg.vector_store.user_scope,
            cfg.vector_store.environment,
        )
        raw_id = f"{request_id}:{summary_id}"
        indexed_content_hashes[summary_id] = current_hash
        request_ids.append(request_id)
        raw_ids.append(raw_id)
        vectors.append(vector_list)
        point_payloads.append(point_payload)

    if not indexed_content_hashes:
        return 0

    candidate_summary_ids = list(indexed_content_hashes)

    acknowledged = await asyncio.to_thread(
        vector_store.replace_summary_points,
        request_ids,
        raw_ids,
        vectors,
        point_payloads,
    )
    if acknowledged is not True:
        logger.warning(
            "vector_reconcile_summary_batch_unacknowledged",
            extra={"summaries": len(candidate_summary_ids)},
        )
        return 0

    logger.debug(
        "vector_reconcile_summary_batch_upserted",
        extra={
            "summaries": len(candidate_summary_ids),
            "point_ids": [
                summary_point_id(request_id, summary_id)
                for request_id, summary_id in zip(request_ids, candidate_summary_ids, strict=True)
            ],
        },
    )

    indexed_summary_ids = await runtime.embedding_repository.async_mark_summary_embeddings_indexed(
        indexed_content_hashes
    )
    cas_misses = len(indexed_content_hashes) - len(indexed_summary_ids)
    if cas_misses:
        logger.info("vector_reconcile_summary_cas_miss", extra={"rows": cas_misses})
    return len(indexed_summary_ids)
