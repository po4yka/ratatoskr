"""SQLAlchemy read adapter for admin dashboards and audit log views."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, desc, func, select

from app.api.search_helpers import isotime
from app.core.time_utils import UTC
from app.db.models import (
    AuditLog,
    Collection,
    CrawlResult,
    ImportJob,
    LLMCall,
    Request,
    Summary,
    Tag,
    User,
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
