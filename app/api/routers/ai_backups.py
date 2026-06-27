"""AI account backup status endpoints (read-only).

Exposes the lifecycle state of the operator's ChatGPT/Claude account backups and
accepts a Mode A session blob (Playwright ``storage_state``) for a service. The
backup itself runs in the Taskiq ``ratatoskr.ai_backup.sync`` job.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — used at runtime by the FastAPI response schema
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.routers.auth import get_current_user
from app.core.logging_utils import get_logger
from app.db.models.ai_backup import (  # noqa: TC001 — FastAPI resolves path-param types at runtime
    AiBackupService,
)
from app.db.session import (  # noqa: TC001 — used at runtime in FastAPI Depends() signatures
    Database,
)

if TYPE_CHECKING:
    from app.adapters.ai_backup.repository import AiBackupRepository
    from app.db.models.ai_backup import AiAccountBackup

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/ai-backups", tags=["ai-backups"])


class AiBackupItem(BaseModel):
    """Lifecycle state of one service's backup."""

    service: str = Field(description="chatgpt | claude")
    status: str = Field(description="pending | ok | failed | auth_expired | disabled")
    last_backed_up_at: dt.datetime | None = None
    last_attempt_at: dt.datetime | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    last_error_category: str | None = None
    counts: dict[str, Any] | None = None


class AiBackupListResponse(BaseModel):
    """All AI account backups for the authenticated user."""

    backups: list[AiBackupItem]


class SessionIngestRequest(BaseModel):
    """Body for ``POST /{service}/session`` (Mode A session ingest)."""

    storage_state: dict = Field(
        description=(
            "Full Playwright storage_state object with a 'cookies' list "
            "(and optional 'origins'), exported from a browser already logged "
            "into the target service. Never echoed back in any response."
        )
    )


def _get_db(request: Request) -> Database:
    from app.api.dependencies.database import get_session_manager

    return get_session_manager(request)


def _get_repo(request: Request) -> AiBackupRepository:
    from app.adapters.ai_backup.repository import AiBackupRepository

    return AiBackupRepository(_get_db(request))


def _to_item(row: AiAccountBackup) -> AiBackupItem:
    return AiBackupItem(
        service=row.service.value if hasattr(row.service, "value") else str(row.service),
        status=row.status.value if hasattr(row.status, "value") else str(row.status),
        last_backed_up_at=row.last_backed_up_at,
        last_attempt_at=row.last_attempt_at,
        consecutive_failures=row.consecutive_failures,
        last_error=row.last_error,
        last_error_category=row.last_error_category,
        counts=row.counts_json,
    )


@router.get("", response_model=AiBackupListResponse)
async def list_ai_backups(
    user: dict[str, Any] = Depends(get_current_user),
    repo: AiBackupRepository = Depends(_get_repo),
) -> AiBackupListResponse:
    """List the authenticated user's AI account backup status rows."""
    user_id: int = user["user_id"]
    rows = await repo.list_for_user(user_id)
    return AiBackupListResponse(backups=[_to_item(r) for r in rows])


@router.get("/{service}", response_model=AiBackupItem)
async def get_ai_backup(
    service: AiBackupService,
    user: dict[str, Any] = Depends(get_current_user),
    repo: AiBackupRepository = Depends(_get_repo),
) -> AiBackupItem:
    """Get the backup status for a single service (chatgpt | claude)."""
    user_id: int = user["user_id"]
    row = await repo.get(user_id, service)
    if row is None:
        raise HTTPException(status_code=404, detail="No backup status for this service")
    return _to_item(row)


@router.post("/{service}/session", status_code=204)
async def ingest_session(
    service: AiBackupService,
    body: SessionIngestRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> None:
    """Persist a Playwright browser session for (user, service) — Mode A ingest.

    On success: 204. On bad shape: 400. The storage_state is never echoed back.
    Clears an existing AUTH_EXPIRED halt so the next scheduled run fires.
    """
    from app.adapters.ai_backup.session_store import (
        AiBackupSessionStore,
        _validate_storage_state_shape,
    )
    from app.db.models.ai_backup import AiBackupStatus

    user_id: int = user["user_id"]
    try:
        _validate_storage_state_shape(body.storage_state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db = _get_db(request)
    await AiBackupSessionStore(db).save(user_id, service, body.storage_state)

    repo = _get_repo(request)
    row = await repo.get(user_id, service)
    if row is not None and row.status == AiBackupStatus.AUTH_EXPIRED:
        await repo.record_success(user_id, service)
