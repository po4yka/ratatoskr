"""Admin read queries for LLM usage metrics and cost statistics."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import case, func, or_, select

from app.db.models import CrawlResult, LLMCall, Request
from app.infrastructure.persistence.repositories.admin._helpers import (
    _redact_message,
    isotime,
)

if TYPE_CHECKING:
    import datetime as dt

    from app.db.session import Database


class LLMReadRepository:
    """Read-side queries for LLM call metrics and provider cost breakdown."""

    def __init__(self, database: Database) -> None:
        self._database = database

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

    @staticmethod
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
