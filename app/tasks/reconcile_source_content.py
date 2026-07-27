"""Taskiq convergence job for completed summaries missing Reader source content."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select
from taskiq import TaskiqDepends

from app.application.services.source_content_backfill_service import (
    SourceContentBackfillUnavailableError,
)
from app.config import AppConfig  # noqa: TC001
from app.core.logging_utils import get_logger
from app.db.models import CrawlResult, Request, Summary
from app.db.session import Database  # noqa: TC001
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.observability.metrics_source_content import (
    record_source_content_reconcile_rows,
    record_source_content_reconcile_run,
    set_source_content_missing,
)
from app.tasks.broker import broker
from app.tasks.deps import (
    build_source_content_reconcile_task_runtime,
    get_app_config,
    get_db,
)

logger = get_logger(__name__)

_LOCK_KEY = "task_lock:source_content_reconcile"
_LOCK_TTL_SECONDS = 900
_COMPLETED_STATUSES = ("ok", "complete", "completed", "success", "succeeded")


@dataclass(frozen=True, slots=True)
class SourceContentReconcileSummary:
    scanned: int = 0
    local_repaired: int = 0
    reextracted: int = 0
    skipped: int = 0
    failed: int = 0
    missing_remaining: int = 0


@broker.task(
    task_name="ratatoskr.source_content.reconcile",
    retry_on_error=True,
    max_retries=2,
)
async def reconcile_source_content(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> SourceContentReconcileSummary:
    """Repair legacy drift under a single distributed lock."""
    redis_client = await get_redis(cfg)
    async with RedisDistributedLock(
        redis_client,
        _LOCK_KEY,
        _LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            record_source_content_reconcile_run(status="lock_held")
            return SourceContentReconcileSummary()
        try:
            return await _reconcile_body(cfg, db)
        except Exception:
            record_source_content_reconcile_run(status="error")
            raise


async def _reconcile_body(
    cfg: AppConfig,
    db: Database,
    *,
    service: Any | None = None,
    correlation_id: str | None = None,
) -> SourceContentReconcileSummary:
    retention = cfg.retention
    if not retention.source_content_reconcile_enabled or retention.privacy_no_retention_mode:
        set_source_content_missing(0)
        record_source_content_reconcile_run(status="disabled")
        return SourceContentReconcileSummary()

    rows = await _fetch_missing_source_rows(
        db,
        limit=retention.source_content_reconcile_batch_size,
    )
    missing_total = int(rows[0]["missing_total"]) if rows else 0
    set_source_content_missing(missing_total)
    if not rows:
        summary = SourceContentReconcileSummary()
        _record_summary(summary, status="success")
        return summary

    if service is None:
        service = build_source_content_reconcile_task_runtime(cfg, db).service

    cid = correlation_id or f"source-content-reconcile-{uuid4()}"
    local_repaired = 0
    reextracted = 0
    network_attempts = 0
    skipped = 0
    failed = 0
    for row in rows:
        allow_reextract = False
        if not row["has_local_source"]:
            allow_reextract = network_attempts < retention.source_content_reconcile_network_limit
            if allow_reextract:
                network_attempts += 1
        try:
            result = await service.backfill(
                user_id=int(row["user_id"]),
                summary_id=int(row["summary_id"]),
                operation_correlation_id=cid,
                allow_reextract=allow_reextract,
            )
        except SourceContentBackfillUnavailableError:
            skipped += 1
        except Exception as exc:
            failed += 1
            logger.warning(
                "source_content_reconcile_row_failed",
                extra={
                    "summary_id": row["summary_id"],
                    "request_id": row["request_id"],
                    "cid": cid,
                    "error_type": type(exc).__name__,
                },
            )
        else:
            if result.reextracted:
                reextracted += 1
            else:
                local_repaired += 1

    repaired = local_repaired + reextracted
    summary = SourceContentReconcileSummary(
        scanned=len(rows),
        local_repaired=local_repaired,
        reextracted=reextracted,
        skipped=skipped,
        failed=failed,
        missing_remaining=max(0, missing_total - repaired),
    )
    set_source_content_missing(summary.missing_remaining)
    _record_summary(summary, status="success" if failed == 0 else "error")
    logger.info(
        "source_content_reconcile_complete",
        extra={**asdict(summary), "cid": cid},
    )
    return summary


async def _fetch_missing_source_rows(
    db: Database,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    missing_content = or_(
        CrawlResult.id.is_(None),
        func.length(func.btrim(func.coalesce(CrawlResult.content_text, ""))) == 0,
    )
    request_content = func.btrim(func.coalesce(Request.content_text, ""))
    has_local_source = or_(
        func.length(func.btrim(func.coalesce(CrawlResult.content_markdown, ""))) > 0,
        func.length(func.btrim(func.coalesce(CrawlResult.content_html, ""))) > 0,
        (
            (func.length(request_content) > 0)
            & (request_content != func.coalesce(Request.input_url, ""))
            & (request_content != func.coalesce(Request.normalized_url, ""))
        ),
    )
    stmt = (
        select(
            Summary.id.label("summary_id"),
            Request.id.label("request_id"),
            Request.user_id,
            has_local_source.label("has_local_source"),
            func.count().over().label("missing_total"),
        )
        .join(Request, Request.id == Summary.request_id)
        .outerjoin(CrawlResult, CrawlResult.request_id == Request.id)
        .where(
            Request.type == "url",
            Request.status.in_(_COMPLETED_STATUSES),
            Request.is_deleted.is_(False),
            Summary.is_deleted.is_(False),
            Request.user_id.is_not(None),
            missing_content,
        )
        .order_by(Request.created_at, Request.id)
        .limit(limit)
    )
    async with db.session() as session:
        return [dict(row) for row in (await session.execute(stmt)).mappings()]


def _record_summary(
    summary: SourceContentReconcileSummary,
    *,
    status: str,
) -> None:
    record_source_content_reconcile_rows(
        scanned=summary.scanned,
        local_repaired=summary.local_repaired,
        reextracted=summary.reextracted,
        skipped=summary.skipped,
        failed=summary.failed,
    )
    record_source_content_reconcile_run(status=status)
