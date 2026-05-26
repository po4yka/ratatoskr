"""System maintenance endpoints."""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from app.api.dependencies.database import get_session_manager
from app.api.models.responses import success_response
from app.api.routers.auth import get_current_user
from app.api.services.auth_service import AuthService
from app.api.services.system_maintenance_service import SystemMaintenanceService, _silent_unlink
from app.core.logging_utils import get_logger
from app.di.shared import build_async_audit_sink

router = APIRouter()
logger = get_logger(__name__)


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

    dump_file = service.build_db_dump_file(user_id=user_id)

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

    dump_file = service.build_db_dump_file(user_id=user_id)

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
