"""Taskiq convergence job for completed summaries missing Reader source content."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
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
    set_source_content_oldest_missing_age_seconds,
)
from app.tasks.broker import broker
from app.tasks.deps import (
    build_source_content_reconcile_task_runtime,
    get_app_config,
    get_db,
)

logger = get_logger(__name__)

_LOCK_KEY = "task_lock:source_content_reconcile"
_CURSOR_KEY = "task_cursor:source_content_reconcile"
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
    next_cursor: int = 0


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
            cursor = await _read_cursor(redis_client)
            summary = await _reconcile_body(cfg, db, after_summary_id=cursor)
            await _write_cursor(redis_client, summary.next_cursor)
            return summary
        except Exception:
            record_source_content_reconcile_run(status="error")
            raise


async def _reconcile_body(
    cfg: AppConfig,
    db: Database,
    *,
    service: Any | None = None,
    correlation_id: str | None = None,
    batch_size: int | None = None,
    network_limit: int | None = None,
    after_summary_id: int = 0,
) -> SourceContentReconcileSummary:
    retention = cfg.retention
    if not retention.source_content_reconcile_enabled or retention.privacy_no_retention_mode:
        set_source_content_missing(0)
        set_source_content_oldest_missing_age_seconds(0)
        record_source_content_reconcile_run(status="disabled")
        return SourceContentReconcileSummary()

    rows = await _fetch_missing_source_rows(
        db,
        limit=batch_size or retention.source_content_reconcile_batch_size,
        after_summary_id=after_summary_id,
    )
    if not rows and after_summary_id > 0:
        rows = await _fetch_missing_source_rows(
            db,
            limit=batch_size or retention.source_content_reconcile_batch_size,
        )
    if not rows:
        missing_total, oldest_missing_age_seconds = await _get_missing_source_stats(db)
        set_source_content_missing(missing_total)
        set_source_content_oldest_missing_age_seconds(oldest_missing_age_seconds)
        summary = SourceContentReconcileSummary()
        _record_summary(summary, status="success")
        return summary

    if service is None:
        service = build_source_content_reconcile_task_runtime(cfg, db).service

    cid = correlation_id or f"source-content-reconcile-{uuid4()}"
    local_repaired = 0
    reextracted = 0
    network_attempts = 0
    network_budget = (
        retention.source_content_reconcile_network_limit if network_limit is None else network_limit
    )
    skipped = 0
    failed = 0
    for row in rows:
        allow_reextract = not row["has_local_source"] and network_attempts < network_budget
        if allow_reextract:
            network_attempts += 1
        if row["user_id"] is None:
            backfill = service.backfill_for_reconciliation
            backfill_kwargs = {
                "summary_id": int(row["summary_id"]),
                "operation_correlation_id": cid,
            }
        else:
            backfill = service.backfill
            backfill_kwargs = {
                "user_id": int(row["user_id"]),
                "summary_id": int(row["summary_id"]),
                "operation_correlation_id": cid,
            }
        while True:
            try:
                result = await backfill(
                    **backfill_kwargs,
                    allow_reextract=allow_reextract,
                )
            except SourceContentBackfillUnavailableError:
                if (
                    row["has_local_source"]
                    and not allow_reextract
                    and network_attempts < network_budget
                ):
                    allow_reextract = True
                    network_attempts += 1
                    continue
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
            break

    missing_remaining, oldest_missing_age_seconds = await _get_missing_source_stats(db)
    summary = SourceContentReconcileSummary(
        scanned=len(rows),
        local_repaired=local_repaired,
        reextracted=reextracted,
        skipped=skipped,
        failed=failed,
        missing_remaining=missing_remaining,
        next_cursor=int(rows[-1]["summary_id"]),
    )
    set_source_content_missing(summary.missing_remaining)
    set_source_content_oldest_missing_age_seconds(oldest_missing_age_seconds)
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
    after_summary_id: int = 0,
) -> list[dict[str, Any]]:
    stmt = (
        select(
            Summary.id.label("summary_id"),
            Request.id.label("request_id"),
            Request.user_id,
            _has_local_source_predicate().label("has_local_source"),
        )
        .join(Request, Request.id == Summary.request_id)
        .outerjoin(CrawlResult, CrawlResult.request_id == Request.id)
        .where(
            *_missing_source_filters(),
            Summary.id > max(0, after_summary_id),
        )
        .order_by(Summary.id)
        .limit(limit)
    )
    async with db.session() as session:
        return [dict(row) for row in (await session.execute(stmt)).mappings()]


async def _get_missing_source_stats(db: Database) -> tuple[int, float]:
    stmt = (
        select(func.count(), func.min(Request.created_at))
        .select_from(Summary)
        .join(Request, Request.id == Summary.request_id)
        .outerjoin(CrawlResult, CrawlResult.request_id == Request.id)
        .where(*_missing_source_filters())
    )
    async with db.session() as session:
        missing_total, oldest_created_at = (await session.execute(stmt)).one()
    if not oldest_created_at:
        return int(missing_total), 0
    if oldest_created_at.tzinfo is None:
        oldest_created_at = oldest_created_at.replace(tzinfo=UTC)
    age_seconds = (datetime.now(UTC) - oldest_created_at).total_seconds()
    return int(missing_total), max(0, age_seconds)


def _missing_source_filters() -> tuple[Any, ...]:
    missing_content = or_(
        CrawlResult.id.is_(None),
        func.length(func.btrim(func.coalesce(CrawlResult.content_text, ""))) == 0,
    )
    return (
        Request.type == "url",
        Request.status.in_(_COMPLETED_STATUSES),
        Request.is_deleted.is_(False),
        Summary.is_deleted.is_(False),
        missing_content,
    )


def _has_local_source_predicate() -> Any:
    request_content = func.btrim(func.coalesce(Request.content_text, ""))
    return or_(
        func.length(func.btrim(func.coalesce(CrawlResult.content_markdown, ""))) > 0,
        func.length(func.btrim(func.coalesce(CrawlResult.content_html, ""))) > 0,
        (
            (func.length(request_content) > 0)
            & (request_content != func.coalesce(Request.input_url, ""))
            & (request_content != func.coalesce(Request.normalized_url, ""))
        ),
    )


async def _read_cursor(redis_client: Any) -> int:
    try:
        raw_cursor = await redis_client.get(_CURSOR_KEY)
        if raw_cursor is None:
            return 0
        return max(0, int(raw_cursor))
    except (TypeError, ValueError):
        logger.warning("source_content_reconcile_cursor_invalid")
        return 0
    except Exception as exc:
        logger.warning(
            "source_content_reconcile_cursor_read_failed",
            extra={"error_type": type(exc).__name__},
        )
        return 0


async def _write_cursor(redis_client: Any, cursor: int) -> None:
    try:
        await redis_client.set(_CURSOR_KEY, str(max(0, cursor)))
    except Exception as exc:
        logger.warning(
            "source_content_reconcile_cursor_write_failed",
            extra={"error_type": type(exc).__name__},
        )


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
