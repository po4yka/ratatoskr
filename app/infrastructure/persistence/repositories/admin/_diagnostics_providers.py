"""Admin diagnostics: LLM providers, scraper providers, vector lag, queue backlog, storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import case, func, or_, select

from app.db.models import (
    CrawlResult,
    LLMCall,
    Request,
    RequestProcessingJob,
    RSSFeed,
    Summary,
    SummaryEmbedding,
    UserGitHubIntegration,
)
from app.infrastructure.persistence.repositories.admin._helpers import _redact_message

if TYPE_CHECKING:
    import datetime as dt


async def _queue_backlog(session: Any, *, now: dt.datetime) -> dict[str, Any]:
    rows = await session.execute(
        select(RequestProcessingJob.status, func.count(RequestProcessingJob.id)).group_by(
            RequestProcessingJob.status
        )
    )
    by_status = {str(status or "unknown"): int(count or 0) for status, count in rows}
    runnable = await session.scalar(
        select(func.count(RequestProcessingJob.id)).where(
            or_(
                RequestProcessingJob.status == "queued",
                (
                    (RequestProcessingJob.status == "failed")
                    & (
                        (RequestProcessingJob.retry_after.is_(None))
                        | (RequestProcessingJob.retry_after <= now)
                    )
                ),
                (
                    (RequestProcessingJob.status == "running")
                    & (RequestProcessingJob.lease_expires_at <= now)
                ),
            ),
            RequestProcessingJob.attempt_count < RequestProcessingJob.max_attempts,
        )
    )
    expired_running = await session.scalar(
        select(func.count(RequestProcessingJob.id)).where(
            RequestProcessingJob.status == "running",
            RequestProcessingJob.lease_expires_at.is_not(None),
            RequestProcessingJob.lease_expires_at <= now,
        )
    )
    oldest_queued = await session.scalar(
        select(func.min(RequestProcessingJob.created_at)).where(
            RequestProcessingJob.status == "queued"
        )
    )
    oldest_retry = await session.scalar(
        select(func.min(RequestProcessingJob.retry_after)).where(
            RequestProcessingJob.status == "failed",
            RequestProcessingJob.retry_after.is_not(None),
        )
    )
    return {
        "by_status": by_status,
        "runnable_count": int(runnable or 0),
        "oldest_queued_at": oldest_queued,
        "oldest_retry_after": oldest_retry,
        "expired_running_leases": int(expired_running or 0),
    }


async def _vector_indexing_lag(session: Any) -> dict[str, Any]:
    missing = await session.scalar(
        select(func.count(Summary.id))
        .outerjoin(SummaryEmbedding, SummaryEmbedding.summary_id == Summary.id)
        .where(Summary.is_deleted.is_(False), SummaryEmbedding.id.is_(None))
    )
    stale = await session.scalar(
        select(func.count(Summary.id))
        .join(SummaryEmbedding, SummaryEmbedding.summary_id == Summary.id)
        .where(
            Summary.is_deleted.is_(False),
            or_(
                SummaryEmbedding.last_indexed_at.is_(None),
                SummaryEmbedding.last_indexed_at < Summary.updated_at,
            ),
        )
    )
    pending = await session.scalar(
        select(func.count(SummaryEmbedding.id)).where(SummaryEmbedding.index_status != "indexed")
    )
    oldest_unindexed = await session.scalar(
        select(func.min(Summary.updated_at))
        .outerjoin(SummaryEmbedding, SummaryEmbedding.summary_id == Summary.id)
        .where(
            Summary.is_deleted.is_(False),
            or_(
                SummaryEmbedding.id.is_(None),
                SummaryEmbedding.last_indexed_at.is_(None),
                SummaryEmbedding.last_indexed_at < Summary.updated_at,
            ),
        )
    )
    latest_indexed = await session.scalar(select(func.max(SummaryEmbedding.last_indexed_at)))
    missing_int = int(missing or 0)
    stale_int = int(stale or 0)
    pending_int = int(pending or 0)
    status = "healthy" if missing_int + stale_int + pending_int == 0 else "degraded"
    return {
        "status": status,
        "missing_embeddings": missing_int,
        "stale_embeddings": stale_int,
        "pending_embeddings": pending_int,
        "oldest_unindexed_summary_updated_at": oldest_unindexed,
        "latest_indexed_at": latest_indexed,
    }


async def _llm_provider_stats(session: Any, *, since: dt.datetime) -> list[dict[str, Any]]:
    failure_expr = case(
        (
            LLMCall.status.notin_(("ok", "success", "completed", "succeeded")),
            1,
        ),
        else_=0,
    )
    provider_totals = (
        select(
            LLMCall.provider.label("provider"),
            func.count(LLMCall.id).label("total_count"),
            func.sum(failure_expr).label("failure_count"),
        )
        .where(LLMCall.created_at >= since)
        .group_by(LLMCall.provider)
        .subquery()
    )
    latest_failures = (
        select(
            LLMCall.provider.label("provider"),
            LLMCall.error_text.label("error_text"),
            LLMCall.status.label("status"),
            LLMCall.updated_at.label("updated_at"),
            func.row_number()
            .over(partition_by=LLMCall.provider, order_by=LLMCall.updated_at.desc())
            .label("row_number"),
        )
        .where(
            LLMCall.created_at >= since,
            LLMCall.status.notin_(("ok", "success", "completed", "succeeded")),
        )
        .subquery()
    )
    latest_provider_matches = or_(
        latest_failures.c.provider == provider_totals.c.provider,
        latest_failures.c.provider.is_(None) & provider_totals.c.provider.is_(None),
    )
    rows = (
        await session.execute(
            select(
                provider_totals.c.provider,
                provider_totals.c.total_count,
                provider_totals.c.failure_count,
                latest_failures.c.error_text,
                latest_failures.c.status,
                latest_failures.c.updated_at,
            )
            .outerjoin(
                latest_failures,
                latest_provider_matches & (latest_failures.c.row_number == 1),
            )
            .select_from(provider_totals)
        )
    ).all()
    stats: list[dict[str, Any]] = []
    for provider, total, failures, error_text, status, updated_at in rows:
        provider_name = str(provider or "unknown")
        failure_count = int(failures or 0)
        stats.append(
            {
                "provider": provider_name,
                "status": "healthy" if failure_count == 0 else "degraded",
                "total_count": int(total or 0),
                "failure_count": failure_count,
                "last_error_code": str(status or "LLM_FAILURE") if failure_count else None,
                "last_error_message": _redact_message(error_text),
                "last_failure_at": updated_at,
            }
        )
    return stats


async def _scraper_provider_stats(session: Any, *, since: dt.datetime) -> list[dict[str, Any]]:
    provider_expr = func.coalesce(CrawlResult.winning_provider, CrawlResult.endpoint, "unknown")
    failure_condition = or_(
        CrawlResult.firecrawl_success.is_(False),
        CrawlResult.error_text.is_not(None),
        CrawlResult.firecrawl_error_code.is_not(None),
        CrawlResult.status.in_(("error", "failed", "timeout")),
    )
    provider_totals = (
        select(
            provider_expr.label("provider"),
            func.count(CrawlResult.id).label("total_count"),
            func.sum(case((failure_condition, 1), else_=0)).label("failure_count"),
        )
        .where(CrawlResult.updated_at >= since)
        .group_by(provider_expr)
        .subquery()
    )
    latest_failures = (
        select(
            provider_expr.label("provider"),
            CrawlResult.firecrawl_error_code.label("firecrawl_error_code"),
            CrawlResult.error_text.label("error_text"),
            CrawlResult.firecrawl_error_message.label("firecrawl_error_message"),
            CrawlResult.updated_at.label("updated_at"),
            func.row_number()
            .over(partition_by=provider_expr, order_by=CrawlResult.updated_at.desc())
            .label("row_number"),
        )
        .where(CrawlResult.updated_at >= since, failure_condition)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                provider_totals.c.provider,
                provider_totals.c.total_count,
                provider_totals.c.failure_count,
                latest_failures.c.firecrawl_error_code,
                latest_failures.c.error_text,
                latest_failures.c.firecrawl_error_message,
                latest_failures.c.updated_at,
            )
            .outerjoin(
                latest_failures,
                (latest_failures.c.provider == provider_totals.c.provider)
                & (latest_failures.c.row_number == 1),
            )
            .select_from(provider_totals)
        )
    ).all()
    stats: list[dict[str, Any]] = []
    for (
        provider,
        total,
        failures,
        error_code,
        error_text,
        firecrawl_error,
        updated_at,
    ) in rows:
        provider_name = str(provider or "unknown")
        failure_count = int(failures or 0)
        stats.append(
            {
                "provider": provider_name,
                "status": "healthy" if failure_count == 0 else "degraded",
                "total_count": int(total or 0),
                "failure_count": failure_count,
                "last_error_code": str(error_code or "SCRAPER_FAILURE") if failure_count else None,
                "last_error_message": _redact_message(error_text or firecrawl_error),
                "last_failure_at": updated_at,
            }
        )
    return stats


async def _integration_health(session: Any) -> dict[str, Any]:
    rss_total = int(await session.scalar(select(func.count(RSSFeed.id))) or 0)
    rss_failing = int(
        await session.scalar(
            select(func.count(RSSFeed.id)).where(
                RSSFeed.is_active.is_(True),
                or_(RSSFeed.fetch_error_count > 0, RSSFeed.last_error.is_not(None)),
            )
        )
        or 0
    )
    github_total = int(await session.scalar(select(func.count(UserGitHubIntegration.id))) or 0)
    github_failing = int(
        await session.scalar(
            select(func.count(UserGitHubIntegration.id)).where(
                UserGitHubIntegration.status != "active"
            )
        )
        or 0
    )
    return {
        "rss": {
            "status": "healthy" if rss_failing == 0 else "degraded",
            "total_count": rss_total,
            "failure_count": rss_failing,
        },
        "github": {
            "status": "healthy" if github_failing == 0 else "degraded",
            "total_count": github_total,
            "failure_count": github_failing,
        },
    }


async def _storage_activity(
    session: Any,
    *,
    last_24h: dt.datetime,
    last_7d: dt.datetime,
) -> dict[str, Any]:
    models: dict[str, Any] = {
        "requests": Request,
        "summaries": Summary,
        "crawl_results": CrawlResult,
        "llm_calls": LLMCall,
        "request_processing_jobs": RequestProcessingJob,
        "rss_feeds": RSSFeed,
    }
    created_last_24h: dict[str, int] = {}
    created_last_7d: dict[str, int] = {}
    for name, model in models.items():
        created_at = getattr(model, "created_at", None)
        if created_at is None:
            continue
        model_id = cast("Any", model).id
        # Both time-window counts scan the same table; combine with FILTER aggregates.
        row = (
            await session.execute(
                select(
                    func.count(model_id).filter(created_at >= last_24h).label("cnt_24h"),
                    func.count(model_id).filter(created_at >= last_7d).label("cnt_7d"),
                )
            )
        ).one()
        created_last_24h[name] = int(row.cnt_24h or 0)
        created_last_7d[name] = int(row.cnt_7d or 0)
    return {
        "created_last_24h": created_last_24h,
        "created_last_7d": created_last_7d,
    }
