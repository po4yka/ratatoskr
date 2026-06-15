"""Read-side service for admin API endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.api.dependencies.database import get_session_manager
from app.api.services.system_maintenance_service import SystemMaintenanceService
from app.core.llm_usage_budget import LLMUsageSnapshot, evaluate_aggregate_budget
from app.db.session import Database  # noqa: TC001  # used at runtime in __init__ signature
from app.infrastructure.persistence.repositories.admin_read_repository import (
    AdminReadRepositoryAdapter,
)

if TYPE_CHECKING:
    import datetime as _dt


class AdminReadService:
    """Owns admin dashboards and audit log read models."""

    def __init__(
        self,
        session_manager: Database | None = None,
    ) -> None:
        self._db = session_manager or get_session_manager()
        self._admin_repo = AdminReadRepositoryAdapter(self._db)

    async def list_users(self) -> dict[str, Any]:
        return await self._admin_repo.async_list_users()

    async def job_status(self, *, today: _dt.datetime) -> dict[str, Any]:
        return await self._admin_repo.async_job_status(today=today)

    async def content_health(self) -> dict[str, Any]:
        return await self._admin_repo.async_content_health()

    async def system_metrics(self, *, since: _dt.datetime) -> dict[str, Any]:
        metrics = await self._admin_repo.async_system_metrics(since=since)
        metrics["database"] = await SystemMaintenanceService(database=self._db).get_db_info()
        return metrics

    async def llm_cost_stats(
        self,
        *,
        since: _dt.datetime,
        today: _dt.datetime,
        month_start: _dt.datetime,
        budget: Any | None = None,
    ) -> dict[str, Any]:
        stats = await self._admin_repo.async_llm_cost_stats(
            since=since,
            today=today,
            month_start=month_start,
        )
        if budget is not None:
            usage = LLMUsageSnapshot(
                daily_cost_usd=float(stats["periods"]["today_cost_usd"]),
                monthly_cost_usd=float(stats["periods"]["month_cost_usd"]),
            )
            decision = evaluate_aggregate_budget(budget=budget, usage=usage)
            stats["budget"] = {
                "status": decision.status,
                "hard_stop": decision.hard_stop,
                "warning": decision.warning,
                "reasons": list(decision.reasons),
                "limits": {
                    "max_tokens_per_request": budget.max_tokens_per_request,
                    "max_cost_usd_per_request": budget.max_cost_usd_per_request,
                    "daily_soft_budget_usd": budget.daily_soft_budget_usd,
                    "monthly_soft_budget_usd": budget.monthly_soft_budget_usd,
                    "warning_threshold_ratio": budget.warning_threshold_ratio,
                    "daily_hard_budget_usd": budget.daily_hard_budget_usd,
                    "monthly_hard_budget_usd": budget.monthly_hard_budget_usd,
                },
            }
        return stats

    async def audit_log(
        self,
        *,
        action: str | None,
        user_id_filter: int | None,
        since: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        return await self._admin_repo.async_audit_log(
            action=action,
            user_id_filter=user_id_filter,
            since=since,
            limit=limit,
            offset=offset,
        )
