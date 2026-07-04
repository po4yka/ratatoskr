"""System maintenance endpoints."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from app.api.dependencies.database import get_session_manager
from app.api.exceptions import APIException, ErrorCode
from app.api.models.responses import success_response
from app.api.routers.auth import get_current_user
from app.api.services.auth_service import AuthService
from app.api.services.system_maintenance_service import SystemMaintenanceService, _silent_unlink
from app.core.logging_utils import get_logger
from app.di.shared import build_async_audit_sink
from app.security.rate_limiter import RateLimitConfig, RedisUserRateLimiter, UserRateLimiter

router = APIRouter()
logger = get_logger(__name__)

# GET and HEAD /v1/system/db-dump both run a full pg_dump; cap at 3/hour per
# owner regardless of which verb is used, so HEAD probes count against the
# same budget as full downloads.
_DB_DUMP_RATE_CONFIG = RateLimitConfig(max_requests=3, window_seconds=3600)
_DB_DUMP_BUCKET = "db_dump"

# Per-process fallback used only when the shared Redis client is unavailable.
# Must be a module-level singleton: constructing a fresh UserRateLimiter per
# request would retain no memory of prior requests and would not limit
# anything. Still atomic within this worker via UserRateLimiter's internal
# asyncio.Lock.
_db_dump_local_limiter = UserRateLimiter(_DB_DUMP_RATE_CONFIG)


def _resolve_db(request: Any) -> Any:
    """Resolve DB handle for audit sinks, falling back to session manager."""
    from app.di.api import resolve_api_runtime

    with contextlib.suppress(RuntimeError):
        return resolve_api_runtime(request).db
    return get_session_manager(request)


def get_system_maintenance_service() -> SystemMaintenanceService:
    """FastAPI dependency provider for maintenance service."""
    return SystemMaintenanceService()


def _extract_user_id(user: dict[str, Any]) -> int:
    raw_user_id = user.get("user_id")
    if isinstance(raw_user_id, bool) or not isinstance(raw_user_id, int):
        raise ValueError("Authenticated user payload is missing integer user_id")
    # int(int) is a no-op at runtime and satisfies both mypy 1.x (where
    # raw_user_id is still Any after the isinstance check) and mypy 2.x
    # (which narrows but rejects redundant casts).
    return int(raw_user_id)


def _resolve_db_dump_limiter(request: Request) -> RedisUserRateLimiter | UserRateLimiter:
    """Resolve the atomic limiter that gates db-dump requests.

    Reuses the process-shared Redis client already wired into the API
    runtime by DI (the same handle the rest of the API relies on) so the
    limit is enforced across workers. Falls back to the per-process
    in-memory limiter only when Redis is unavailable.
    """
    from app.di.api import resolve_api_runtime

    with contextlib.suppress(RuntimeError):
        runtime = resolve_api_runtime(request)
        if runtime.redis_client is not None:
            return RedisUserRateLimiter(
                runtime.redis_client,
                _DB_DUMP_RATE_CONFIG,
                prefix=f"{runtime.cfg.redis.prefix}:{_DB_DUMP_BUCKET}",
            )
    return _db_dump_local_limiter


async def _enforce_db_dump_rate_limit(request: Request, user_id: int) -> None:
    """Atomically reserve a db-dump slot before running pg_dump.

    This MUST run before the expensive pg_dump subprocess, not after.
    Counting completed dumps via an after-the-fact audit-log row is racy:
    that row is written fire-and-forget once pg_dump finishes, so N
    concurrent requests all read the same stale count and all pass.
    ``check_and_record`` instead atomically increments-and-checks in a
    single round-trip (Redis INCR+EXPIRE pipeline, or a lock-guarded
    in-memory counter), so only the first 3 requests per rolling hour ever
    reach pg_dump.
    """
    limiter = _resolve_db_dump_limiter(request)
    allowed, error_msg = await limiter.check_and_record(user_id, operation=_DB_DUMP_BUCKET)
    if not allowed:
        raise APIException(
            message=error_msg
            or f"Rate limit exceeded: maximum {_DB_DUMP_RATE_CONFIG.max_requests} "
            f"database dumps per {_DB_DUMP_RATE_CONFIG.window_seconds} seconds",
            error_code=ErrorCode.RATE_LIMIT_EXCEEDED,
            status_code=429,
        )


@router.get("/db-dump")
async def download_database(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    service: SystemMaintenanceService = Depends(get_system_maintenance_service),
) -> Any:
    """
    Download a consistent PostgreSQL backup dump.

    Requires owner permissions.
    """
    await AuthService.require_owner(user)  # type: ignore[arg-type]
    user_id = _extract_user_id(user)
    await _enforce_db_dump_rate_limit(request, user_id)

    # build_db_dump_file runs pg_dump (subprocess) + sync file I/O; keep it off the loop.
    dump_file = await asyncio.to_thread(service.build_db_dump_file, user_id=user_id)

    audit = build_async_audit_sink(_resolve_db(request))
    audit("INFO", "admin.db_dump", {"user_id": user_id})

    return FileResponse(
        path=dump_file.path,
        filename=dump_file.filename,
        media_type=dump_file.media_type,
        background=BackgroundTask(_silent_unlink, dump_file.path),
    )


@router.head("/db-dump")
async def head_database(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    service: SystemMaintenanceService = Depends(get_system_maintenance_service),
) -> Any:
    """HEAD variant for clients that only need headers before downloading."""
    await AuthService.require_owner(user)  # type: ignore[arg-type]
    user_id = _extract_user_id(user)
    await _enforce_db_dump_rate_limit(request, user_id)

    # build_db_dump_file runs pg_dump (subprocess) + sync file I/O; keep it off the loop.
    dump_file = await asyncio.to_thread(service.build_db_dump_file, user_id=user_id)

    audit = build_async_audit_sink(_resolve_db(request))
    audit("INFO", "admin.db_dump_head", {"user_id": user_id})

    return FileResponse(
        path=dump_file.path,
        filename=dump_file.filename,
        media_type=dump_file.media_type,
        background=BackgroundTask(_silent_unlink, dump_file.path),
    )


@router.get("/db-info")
async def get_db_info(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    service: SystemMaintenanceService = Depends(get_system_maintenance_service),
) -> Any:
    """Get database information: table row counts and file size."""
    await AuthService.require_owner(user)  # type: ignore[arg-type]
    user_id = _extract_user_id(user)
    audit = build_async_audit_sink(_resolve_db(request))
    audit("INFO", "admin.db_info", {"user_id": user_id})
    return success_response(await service.get_db_info())


@router.post("/clear-cache")
async def clear_cache(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    service: SystemMaintenanceService = Depends(get_system_maintenance_service),
) -> Any:
    """Clear Redis URL cache."""
    await AuthService.require_owner(user)  # type: ignore[arg-type]
    user_id = _extract_user_id(user)
    cleared = await service.clear_url_cache()
    audit = build_async_audit_sink(_resolve_db(request))
    audit("INFO", "admin.clear_cache", {"user_id": user_id, "cleared_keys": cleared})
    return success_response({"cleared_keys": cleared})
