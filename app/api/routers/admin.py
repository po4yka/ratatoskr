"""Admin read-only endpoints for system monitoring."""

from __future__ import annotations

import contextlib
import datetime as _dt
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app.api.dependencies.database import get_session_manager
from app.api.models.responses import (
    DiagnosticsSuccessResponse,
    GraphRunEvaluationListSuccessResponse,
    GraphRunLedgerSuccessResponse,
    success_response,
)
from app.api.routers.auth import AuthenticatedUser, get_current_user
from app.api.services.admin_read_service import AdminReadService
from app.api.services.auth_service import AuthService
from app.api.services.diagnostics_service import DiagnosticsService
from app.api.services.graph_run_ledger_service import GraphRunLedgerService
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.di.shared import build_async_audit_sink
from app.observability.metrics import record_admin_diagnostics_request

router = APIRouter()
logger = get_logger(__name__)


def _resolve_db(request: Any) -> Any:
    """Resolve DB handle for audit sinks, falling back to session manager."""
    from app.di.api import resolve_api_runtime

    with contextlib.suppress(RuntimeError):
        return resolve_api_runtime(request).db
    return get_session_manager(request)


def _extract_user_id(user: AuthenticatedUser) -> int:
    return user["user_id"]


def _seven_days_ago() -> _dt.datetime:
    return _dt.datetime.now(UTC) - _dt.timedelta(days=7)


def _days_ago(days: int) -> _dt.datetime:
    return _dt.datetime.now(UTC) - _dt.timedelta(days=days)


def _today_start() -> _dt.datetime:
    return _dt.datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start() -> _dt.datetime:
    return _dt.datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _resolve_llm_budget(request: Any) -> Any | None:
    from app.di.api import resolve_api_runtime

    with contextlib.suppress(RuntimeError):
        return getattr(resolve_api_runtime(request).cfg, "llm_usage_budget", None)
    return None


def _resolve_vector_store(request: Any) -> Any | None:
    from app.di.api import resolve_api_runtime

    with contextlib.suppress(RuntimeError):
        return resolve_api_runtime(request).search.vector_store
    return None


# ---------------------------------------------------------------------------
# 1. GET /users -- List all users with stats
# ---------------------------------------------------------------------------


@router.get("/users")
async def list_users(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Any:
    """List all users with per-user summary/request/tag/collection counts."""
    await AuthService.require_owner(user)
    user_id = _extract_user_id(user)

    audit = build_async_audit_sink(_resolve_db(request))
    audit("INFO", "admin.list_users", {"user_id": user_id})
    return success_response(await AdminReadService(_resolve_db(request)).list_users())


# ---------------------------------------------------------------------------
# 2. GET /jobs -- Background job status
# ---------------------------------------------------------------------------


@router.get("/jobs")
async def job_status(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Any:
    """Pipeline and import job status overview."""
    await AuthService.require_owner(user)
    user_id = _extract_user_id(user)

    audit = build_async_audit_sink(_resolve_db(request))
    audit("INFO", "admin.job_status", {"user_id": user_id})
    service = AdminReadService(_resolve_db(request))
    return success_response(await service.job_status(today=_today_start()))


# ---------------------------------------------------------------------------
# 3. GET /health/content -- Content health report
# ---------------------------------------------------------------------------


@router.get("/health/content")
async def content_health(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Any:
    """Content pipeline health: totals, failure breakdown, recent errors."""
    await AuthService.require_owner(user)
    user_id = _extract_user_id(user)

    audit = build_async_audit_sink(_resolve_db(request))
    audit("INFO", "admin.content_health", {"user_id": user_id})
    return success_response(await AdminReadService(_resolve_db(request)).content_health())


# ---------------------------------------------------------------------------
# 4. GET /metrics -- System metrics
# ---------------------------------------------------------------------------


@router.get("/metrics")
async def system_metrics(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Any:
    """Database, LLM, and scraper metrics."""
    await AuthService.require_owner(user)
    user_id = _extract_user_id(user)

    audit = build_async_audit_sink(_resolve_db(request))
    audit("INFO", "admin.metrics", {"user_id": user_id})
    service = AdminReadService(_resolve_db(request))
    return success_response(await service.system_metrics(since=_seven_days_ago()))


# ---------------------------------------------------------------------------
# 5. GET /llm-costs -- Redacted LLM usage and cost stats
# ---------------------------------------------------------------------------


@router.get("/llm-costs")
async def llm_costs(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    days: int = Query(30, ge=1, le=366),
) -> Any:
    """Redacted aggregate LLM usage and cost statistics."""
    await AuthService.require_owner(user)
    user_id = _extract_user_id(user)

    audit = build_async_audit_sink(_resolve_db(request))
    audit("INFO", "admin.llm_costs", {"user_id": user_id, "days": days})
    service = AdminReadService(_resolve_db(request))
    return success_response(
        await service.llm_cost_stats(
            since=_days_ago(days),
            today=_today_start(),
            month_start=_month_start(),
            budget=_resolve_llm_budget(request),
        )
    )


# ---------------------------------------------------------------------------
# 6. GET /diagnostics -- Owner-only operational diagnostics
# ---------------------------------------------------------------------------


@router.get("/diagnostics", response_model=DiagnosticsSuccessResponse)
async def diagnostics(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Any:
    """Safe provider and operational diagnostics for owner dashboards."""
    await AuthService.require_owner(user)
    user_id = _extract_user_id(user)

    audit = build_async_audit_sink(_resolve_db(request))
    audit("INFO", "admin.diagnostics", {"user_id": user_id})
    service = DiagnosticsService(_resolve_db(request), vector_store=_resolve_vector_store(request))
    response = success_response(await service.diagnostics(request=request))
    record_admin_diagnostics_request("success")
    return response


# ---------------------------------------------------------------------------
# 7. GET /graph-runs -- Owner-only sanitized graph ledger and evaluation records
# ---------------------------------------------------------------------------


@router.get("/graph-runs/{request_id}", response_model=GraphRunLedgerSuccessResponse)
async def graph_run_ledger(
    request_id: int,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Any:
    """Return a sanitized graph chronology, LLM attempts, and feedback signals."""
    await AuthService.require_owner(user)
    user_id = _extract_user_id(user)
    audit = build_async_audit_sink(_resolve_db(request))
    audit("INFO", "admin.graph_run_ledger", {"user_id": user_id, "request_id": request_id})
    return success_response(
        await GraphRunLedgerService(_resolve_db(request)).get_run(request_id=request_id)
    )


@router.get("/graph-runs", response_model=GraphRunEvaluationListSuccessResponse)
async def graph_run_evaluations(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=200),
) -> Any:
    """List bounded privacy-safe run and feedback records for offline evaluation."""
    await AuthService.require_owner(user)
    user_id = _extract_user_id(user)
    audit = build_async_audit_sink(_resolve_db(request))
    audit("INFO", "admin.graph_run_evaluations", {"user_id": user_id, "limit": limit})
    return success_response(
        await GraphRunLedgerService(_resolve_db(request)).list_evaluations(limit=limit)
    )


# ---------------------------------------------------------------------------
# 8. GET /audit-log -- Paginated audit log
# ---------------------------------------------------------------------------


@router.get("/audit-log")
async def audit_log(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    action: str | None = Query(None, description="Filter by event name"),
    user_id_filter: int | None = Query(
        None, alias="user_id", description="Filter by user_id in details"
    ),
    since: str | None = Query(None, description="ISO datetime lower bound"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Any:
    """Paginated, filterable audit log."""
    await AuthService.require_owner(user)
    caller_id = _extract_user_id(user)

    audit = build_async_audit_sink(_resolve_db(request))
    audit("INFO", "admin.audit_log", {"user_id": caller_id})
    service = AdminReadService(_resolve_db(request))
    return success_response(
        await service.audit_log(
            action=action,
            user_id_filter=user_id_filter,
            since=since,
            limit=limit,
            offset=offset,
        )
    )
