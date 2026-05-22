"""SQLAlchemy read adapter for admin dashboards and audit log views."""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import case, desc, func, or_, select

from app.api.search_helpers import isotime
from app.core.time_utils import UTC
from app.db.models import (
    AuditLog,
    Collection,
    CrawlResult,
    ImportJob,
    LLMCall,
    RSSFeed,
    Request,
    RequestProcessingJob,
    Source,
    Summary,
    SummaryEmbedding,
    Tag,
    User,
    UserGitHubIntegration,
)

if TYPE_CHECKING:
    from app.db.session import Database


class AdminReadRepositoryAdapter:
    """Read-side adapter for admin reporting queries."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_list_users(self) -> dict[str, Any]:
        async with self._database.session() as session:
            users_list: list[dict[str, Any]] = []
            users = (await session.execute(select(User).order_by(User.created_at.asc()))).scalars()
            for user in users:
                uid = user.telegram_user_id
                summary_count = await session.scalar(
                    select(func.count(Summary.id))
                    .join(Request, Summary.request_id == Request.id)
                    .where(Request.user_id == uid)
                )
                request_count = await session.scalar(
                    select(func.count(Request.id)).where(Request.user_id == uid)
                )
                tag_count = await session.scalar(
                    select(func.count(Tag.id)).where(Tag.user_id == uid)
                )
                collection_count = await session.scalar(
                    select(func.count(Collection.id)).where(Collection.user_id == uid)
                )
                users_list.append(
                    {
                        "user_id": uid,
                        "username": user.username,
                        "is_owner": user.is_owner,
                        "summary_count": int(summary_count or 0),
                        "request_count": int(request_count or 0),
                        "tag_count": int(tag_count or 0),
                        "collection_count": int(collection_count or 0),
                        "created_at": isotime(user.created_at),
                    }
                )
            return {"users": users_list, "total_users": len(users_list)}

    async def async_job_status(self, *, today: Any) -> dict[str, Any]:
        async with self._database.session() as session:
            pending = await session.scalar(
                select(func.count(Request.id)).where(Request.status == "pending")
            )
            processing = await session.scalar(
                select(func.count(Request.id)).where(
                    Request.status.in_(["crawling", "summarizing", "processing"])
                )
            )
            completed_today = await session.scalar(
                select(func.count(Request.id)).where(
                    Request.status == "completed", Request.updated_at >= today
                )
            )
            failed_today = await session.scalar(
                select(func.count(Request.id)).where(
                    Request.status == "error", Request.updated_at >= today
                )
            )
            imports_active = await session.scalar(
                select(func.count(ImportJob.id)).where(ImportJob.status == "processing")
            )
            imports_completed_today = await session.scalar(
                select(func.count(ImportJob.id)).where(
                    ImportJob.status == "completed", ImportJob.updated_at >= today
                )
            )
            return {
                "pipeline": {
                    "pending": int(pending or 0),
                    "processing": int(processing or 0),
                    "completed_today": int(completed_today or 0),
                    "failed_today": int(failed_today or 0),
                },
                "imports": {
                    "active": int(imports_active or 0),
                    "completed_today": int(imports_completed_today or 0),
                },
            }

    async def async_content_health(self) -> dict[str, Any]:
        async with self._database.session() as session:
            total_summaries = await session.scalar(select(func.count(Summary.id)))
            total_requests = await session.scalar(select(func.count(Request.id)))
            failed_requests = await session.scalar(
                select(func.count(Request.id)).where(Request.status == "error")
            )

            failed_by_error_type: dict[str, int] = {}
            error_groups = await session.execute(
                select(Request.error_type, func.count(Request.id))
                .where(Request.status == "error")
                .group_by(Request.error_type)
            )
            for error_type, count in error_groups:
                key = error_type or "unknown"
                failed_by_error_type[key] = int(count or 0)

            recent_failures: list[dict[str, Any]] = []
            failures = (
                await session.execute(
                    select(Request)
                    .where(Request.status == "error")
                    .order_by(Request.created_at.desc())
                    .limit(20)
                )
            ).scalars()
            for request in failures:
                recent_failures.append(
                    {
                        "id": request.id,
                        "url": request.input_url,
                        "error_type": request.error_type,
                        "error_message": request.error_message,
                        "created_at": isotime(request.created_at),
                    }
                )

            return {
                "total_summaries": int(total_summaries or 0),
                "total_requests": int(total_requests or 0),
                "failed_requests": int(failed_requests or 0),
                "failed_by_error_type": failed_by_error_type,
                "recent_failures": recent_failures,
            }

    async def async_system_metrics(self, *, since: Any) -> dict[str, Any]:
        async with self._database.session() as session:
            llm_total = await session.scalar(
                select(func.count(LLMCall.id)).where(LLMCall.created_at >= since)
            )
            llm_agg = (
                await session.execute(
                    select(
                        func.avg(LLMCall.latency_ms),
                        func.sum(LLMCall.tokens_prompt),
                        func.sum(LLMCall.tokens_completion),
                        func.sum(LLMCall.cost_usd),
                    ).where(LLMCall.created_at >= since)
                )
            ).one()
            llm_errors = await session.scalar(
                select(func.count(LLMCall.id)).where(
                    LLMCall.created_at >= since, LLMCall.status == "error"
                )
            )
            llm_total_int = int(llm_total or 0)
            llm_errors_int = int(llm_errors or 0)
            avg_latency, total_prompt_tokens, total_completion_tokens, total_cost = llm_agg
            llm_stats = {
                "total_calls": llm_total_int,
                "avg_latency_ms": round(float(avg_latency or 0), 1),
                "total_prompt_tokens": int(total_prompt_tokens or 0),
                "total_completion_tokens": int(total_completion_tokens or 0),
                "total_cost_usd": round(float(total_cost or 0), 4),
                "error_rate": round(llm_errors_int / llm_total_int, 4) if llm_total_int else 0.0,
            }

            scraper_rows = await session.execute(
                select(
                    CrawlResult.endpoint,
                    func.count(CrawlResult.id),
                    func.sum(case((CrawlResult.firecrawl_success.is_(True), 1), else_=0)),
                )
                .join(Request, CrawlResult.request_id == Request.id)
                .where(Request.created_at >= since)
                .group_by(CrawlResult.endpoint)
            )
            scraper_stats: dict[str, Any] = {}
            for endpoint, total, success in scraper_rows:
                provider = endpoint or "unknown"
                total_int = int(total or 0)
                success_int = int(success or 0)
                scraper_stats[provider] = {
                    "total": total_int,
                    "success": success_int,
                    "success_rate": round(success_int / total_int, 4) if total_int else 0.0,
                }

            return {"llm_7d": llm_stats, "scraper_7d": scraper_stats}

    async def async_llm_cost_stats(
        self, *, since: Any, today: Any, month_start: Any
    ) -> dict[str, Any]:
        """Return redacted aggregate LLM usage and cost statistics."""
        async with self._database.session() as session:
            total_row = (
                await session.execute(
                    select(
                        func.count(LLMCall.id),
                        func.sum(LLMCall.tokens_prompt),
                        func.sum(LLMCall.tokens_completion),
                        func.sum(LLMCall.cost_usd),
                    ).where(LLMCall.created_at >= since)
                )
            ).one()
            today_cost = await session.scalar(
                select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(
                    LLMCall.created_at >= today
                )
            )
            month_cost = await session.scalar(
                select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(
                    LLMCall.created_at >= month_start
                )
            )
            provider_model_rows = (
                await session.execute(
                    select(
                        LLMCall.provider,
                        LLMCall.model,
                        LLMCall.status,
                        func.count(LLMCall.id),
                        func.sum(LLMCall.tokens_prompt),
                        func.sum(LLMCall.tokens_completion),
                        func.sum(LLMCall.cost_usd),
                        func.avg(LLMCall.latency_ms),
                    )
                    .where(LLMCall.created_at >= since)
                    .group_by(LLMCall.provider, LLMCall.model, LLMCall.status)
                    .order_by(func.coalesce(func.sum(LLMCall.cost_usd), 0.0).desc())
                )
            ).all()

        total_calls, prompt_tokens, completion_tokens, total_cost = total_row
        by_provider_model: list[dict[str, Any]] = []
        for (
            provider,
            model,
            status,
            count,
            prompt,
            completion,
            cost,
            latency,
        ) in provider_model_rows:
            by_provider_model.append(
                {
                    "provider": provider or "unknown",
                    "model": model or "unknown",
                    "status": status or "unknown",
                    "calls": int(count or 0),
                    "prompt_tokens": int(prompt or 0),
                    "completion_tokens": int(completion or 0),
                    "cost_usd": round(float(cost or 0), 6),
                    "avg_latency_ms": round(float(latency or 0), 1),
                }
            )

        return {
            "window_start": isotime(since),
            "totals": {
                "calls": int(total_calls or 0),
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "cost_usd": round(float(total_cost or 0), 6),
            },
            "periods": {
                "today_cost_usd": round(float(today_cost or 0), 6),
                "month_cost_usd": round(float(month_cost or 0), 6),
            },
            "by_provider_model": by_provider_model,
        }

    async def async_audit_log(
        self,
        *,
        action: str | None,
        user_id_filter: int | None,
        since: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        since_dt = _parse_since(since)
        async with self._database.session() as session:
            conditions = []
            if action:
                conditions.append(AuditLog.event == action)
            if since_dt is not None:
                conditions.append(AuditLog.ts >= since_dt)

            query = select(AuditLog)
            count_query = select(func.count(AuditLog.id))
            if conditions:
                query = query.where(*conditions)
                count_query = count_query.where(*conditions)

            total = int(await session.scalar(count_query) or 0)
            logs: list[dict[str, Any]] = []
            entries = (
                await session.execute(query.order_by(desc(AuditLog.ts)).offset(offset).limit(limit))
            ).scalars()
            for entry in entries:
                details = entry.details_json
                if user_id_filter is not None:
                    if not isinstance(details, dict) or details.get("user_id") != user_id_filter:
                        total -= 1
                        continue
                logs.append(
                    {
                        "id": entry.id,
                        "timestamp": isotime(entry.ts),
                        "level": entry.level,
                        "event": entry.event,
                        "details": details,
                    }
                )

            return {"logs": logs, "total": total, "limit": limit, "offset": offset}

    async def async_diagnostics_snapshot(
        self,
        *,
        since: dt.datetime,
        now: dt.datetime,
    ) -> dict[str, Any]:
        """Return redacted operational diagnostics from persisted state."""
        async with self._database.session() as session:
            return {
                "queue_backlog": await self._queue_backlog(session, now=now),
                "vector_indexing_lag": await self._vector_indexing_lag(session),
                "llm_providers": await self._llm_provider_stats(session, since=since),
                "scraper_providers": await self._scraper_provider_stats(session, since=since),
                "integration_health": await self._integration_health(session),
                "latest_sync_failures": await self._latest_sync_failures(session, limit=20),
                "storage_activity": await self._storage_activity(
                    session,
                    last_24h=now - dt.timedelta(days=1),
                    last_7d=now - dt.timedelta(days=7),
                ),
            }

    @staticmethod
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

    @staticmethod
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
            select(func.count(SummaryEmbedding.id)).where(
                SummaryEmbedding.index_status != "indexed"
            )
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

    @staticmethod
    async def _llm_provider_stats(session: Any, *, since: dt.datetime) -> list[dict[str, Any]]:
        failure_expr = case(
            (
                LLMCall.status.notin_(("ok", "success", "completed", "succeeded")),
                1,
            ),
            else_=0,
        )
        rows = (
            await session.execute(
                select(
                    LLMCall.provider,
                    func.count(LLMCall.id),
                    func.sum(failure_expr),
                )
                .where(LLMCall.created_at >= since)
                .group_by(LLMCall.provider)
            )
        ).all()
        stats: list[dict[str, Any]] = []
        for provider, total, failures in rows:
            provider_name = str(provider or "unknown")
            latest = await session.execute(
                select(LLMCall.error_text, LLMCall.status, LLMCall.updated_at)
                .where(
                    LLMCall.created_at >= since,
                    LLMCall.provider.is_(None)
                    if provider is None
                    else LLMCall.provider == provider_name,
                    LLMCall.status.notin_(("ok", "success", "completed", "succeeded")),
                )
                .order_by(LLMCall.updated_at.desc())
                .limit(1)
            )
            error_text, status, updated_at = latest.first() or (None, None, None)
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

    @staticmethod
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

    @staticmethod
    async def _scraper_provider_stats(session: Any, *, since: dt.datetime) -> list[dict[str, Any]]:
        provider_expr = func.coalesce(CrawlResult.winning_provider, CrawlResult.endpoint, "unknown")
        failure_condition = or_(
            CrawlResult.firecrawl_success.is_(False),
            CrawlResult.error_text.is_not(None),
            CrawlResult.firecrawl_error_code.is_not(None),
            CrawlResult.status.in_(("error", "failed", "timeout")),
        )
        rows = (
            await session.execute(
                select(
                    provider_expr.label("provider"),
                    func.count(CrawlResult.id),
                    func.sum(case((failure_condition, 1), else_=0)),
                )
                .where(CrawlResult.updated_at >= since)
                .group_by(provider_expr)
            )
        ).all()
        stats: list[dict[str, Any]] = []
        for provider, total, failures in rows:
            provider_name = str(provider or "unknown")
            latest = await session.execute(
                select(
                    CrawlResult.firecrawl_error_code,
                    CrawlResult.error_text,
                    CrawlResult.firecrawl_error_message,
                    CrawlResult.updated_at,
                )
                .where(
                    CrawlResult.updated_at >= since,
                    provider_expr == provider_name,
                    failure_condition,
                )
                .order_by(CrawlResult.updated_at.desc())
                .limit(1)
            )
            error_code, error_text, firecrawl_error, updated_at = latest.first() or (
                None,
                None,
                None,
                None,
            )
            failure_count = int(failures or 0)
            stats.append(
                {
                    "provider": provider_name,
                    "status": "healthy" if failure_count == 0 else "degraded",
                    "total_count": int(total or 0),
                    "failure_count": failure_count,
                    "last_error_code": str(error_code or "SCRAPER_FAILURE")
                    if failure_count
                    else None,
                    "last_error_message": _redact_message(error_text or firecrawl_error),
                    "last_failure_at": updated_at,
                }
            )
        return stats

    @staticmethod
    async def _latest_sync_failures(session: Any, *, limit: int) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []

        rss_rows = (
            await session.execute(
                select(
                    RSSFeed.id,
                    RSSFeed.fetch_error_count,
                    RSSFeed.last_error,
                    RSSFeed.updated_at,
                )
                .where(or_(RSSFeed.fetch_error_count > 0, RSSFeed.last_error.is_not(None)))
                .order_by(RSSFeed.updated_at.desc())
                .limit(limit)
            )
        ).all()
        for feed_id, error_count, last_error, updated_at in rss_rows:
            failures.append(
                {
                    "source": "rss",
                    "event_id": f"rss-feed:{feed_id}",
                    "error_code": "RSS_FETCH_FAILED",
                    "message": _redact_message(last_error),
                    "occurred_at": updated_at,
                    "retryable": True,
                    "details": {"fetch_error_count": int(error_count or 0)},
                }
            )

        github_rows = (
            await session.execute(
                select(
                    UserGitHubIntegration.id,
                    UserGitHubIntegration.status,
                    UserGitHubIntegration.last_sync_cursor,
                    UserGitHubIntegration.updated_at,
                )
                .where(
                    or_(
                        UserGitHubIntegration.status != "active",
                        UserGitHubIntegration.last_sync_cursor.like(
                            '%"kind": "github_sync_state"%'
                        ),
                    )
                )
                .order_by(UserGitHubIntegration.updated_at.desc())
                .limit(limit)
            )
        ).all()
        for integration_id, status, last_sync_cursor, updated_at in github_rows:
            state = _parse_github_sync_state(last_sync_cursor)
            message = state.get("last_error") or f"GitHub integration status is {status}"
            failures.append(
                {
                    "source": "github",
                    "event_id": f"github-integration:{integration_id}",
                    "error_code": f"GITHUB_{str(status).upper()}",
                    "message": _redact_message(str(message)),
                    "occurred_at": updated_at,
                    "retryable": status == "needs_reauth",
                    "details": {
                        "failure_count": state.get("failure_count"),
                        "backoff_until": state.get("backoff_until"),
                    }
                    if state
                    else {},
                }
            )

        source_rows = (
            await session.execute(
                select(
                    Source.id,
                    Source.kind,
                    Source.fetch_error_count,
                    Source.last_error,
                    Source.updated_at,
                    Source.is_active,
                )
                .where(or_(Source.fetch_error_count > 0, Source.last_error.is_not(None)))
                .order_by(Source.updated_at.desc())
                .limit(limit)
            )
        ).all()
        for source_id, kind, error_count, last_error, updated_at, is_active in source_rows:
            failures.append(
                {
                    "source": "source",
                    "event_id": f"source:{source_id}",
                    "error_code": f"SOURCE_{str(kind).upper()}_FETCH_FAILED",
                    "message": _redact_message(last_error),
                    "occurred_at": updated_at,
                    "retryable": bool(is_active),
                    "details": {
                        "kind": str(kind),
                        "fetch_error_count": int(error_count or 0),
                    },
                }
            )

        import_rows = (
            await session.execute(
                select(ImportJob.id, ImportJob.status, ImportJob.errors_json, ImportJob.updated_at)
                .where(ImportJob.status.in_(("failed", "error")))
                .order_by(ImportJob.updated_at.desc())
                .limit(limit)
            )
        ).all()
        for job_id, status, errors_json, updated_at in import_rows:
            failures.append(
                {
                    "source": "import",
                    "event_id": f"import-job:{job_id}",
                    "error_code": f"IMPORT_{str(status).upper()}",
                    "message": _redact_message(_first_error(errors_json)),
                    "occurred_at": updated_at,
                    "retryable": True,
                    "details": {},
                }
            )

        job_rows = (
            await session.execute(
                select(
                    RequestProcessingJob.id,
                    RequestProcessingJob.request_id,
                    RequestProcessingJob.correlation_id,
                    RequestProcessingJob.last_error_code,
                    RequestProcessingJob.last_error_message,
                    RequestProcessingJob.updated_at,
                    RequestProcessingJob.status,
                )
                .where(RequestProcessingJob.status.in_(("failed", "dead_letter")))
                .order_by(RequestProcessingJob.updated_at.desc())
                .limit(limit)
            )
        ).all()
        for job_id, request_id, correlation_id, error_code, message, updated_at, status in job_rows:
            failures.append(
                {
                    "source": "request",
                    "event_id": f"request-processing-job:{job_id}",
                    "correlation_id": correlation_id,
                    "error_code": error_code or f"REQUEST_JOB_{str(status).upper()}",
                    "message": _redact_message(message),
                    "occurred_at": updated_at,
                    "retryable": status == "failed",
                    "details": {"request_id": int(request_id)},
                }
            )

        return sorted(
            failures,
            key=lambda item: item.get("occurred_at") or dt.datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )[:limit]

    @staticmethod
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
            created_last_24h[name] = int(
                await session.scalar(select(func.count(model_id)).where(created_at >= last_24h))
                or 0
            )
            created_last_7d[name] = int(
                await session.scalar(select(func.count(model_id)).where(created_at >= last_7d)) or 0
            )
        return {
            "created_last_24h": created_last_24h,
            "created_last_7d": created_last_7d,
        }


def _parse_since(since: str | None) -> dt.datetime | None:
    if not since:
        return None
    normalized = since.removesuffix("Z")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(authorization)\s*=\s*(bearer|basic)\s+[a-z0-9._~+/=-]+"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)=([^&\s]+)"),
    re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]+"),
    re.compile(r"(?i)(sk-[a-z0-9_-]{12,})"),
)


def _redact_message(message: Any, *, max_len: int = 240) -> str | None:
    if message is None:
        return None
    text = str(message)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if len(text) > max_len:
        return f"{text[: max_len - 3]}..."
    return text or None


def _parse_github_sync_state(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("kind") != "github_sync_state":
        return {}
    return data


def _first_error(errors_json: Any) -> str | None:
    if isinstance(errors_json, list) and errors_json:
        return str(errors_json[0])
    if isinstance(errors_json, dict):
        for key in ("message", "error", "detail"):
            value = errors_json.get(key)
            if value:
                return str(value)
    return None
