"""AI account backup status and owner-only session lifecycle endpoints.

Exposes the lifecycle state of the operator's ChatGPT/Claude account backups and
accepts a Mode A session blob (Playwright ``storage_state``) for a service. The
backup itself runs in the Taskiq ``ratatoskr.ai_backup.sync`` job.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — used at runtime by the FastAPI response schema
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator

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
    from app.config import AppConfig
    from app.db.models.ai_backup import AiAccountBackup

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/ai-backups", tags=["ai-backups"])


class AiBackupItem(BaseModel):
    """Lifecycle state of one service's backup."""

    service: str = Field(description="chatgpt | claude")
    status: str = Field(
        description="Deprecated combined lifecycle state; use backup_status and authorization_status"
    )
    backup_status: str = Field(description="pending | ok | failed | disabled")
    authorization_status: str = Field(description="missing | unverified | valid | expired")
    authorization_checked_at: dt.datetime | None = None
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


class AiBackupReauthFlow(BaseModel):
    """Public, secret-free state of one temporary interactive login flow."""

    id: str
    service: str
    state: str = Field(
        description=(
            "starting | waiting_for_user | verifying | resuming_backup | completed | "
            "failed | expired | cancelled"
        )
    )
    created_at: dt.datetime
    expires_at: dt.datetime
    error: str | None = None


class ReauthInputRequest(BaseModel):
    """One bounded input event for the owner-only remote browser surface."""

    type: Literal["click", "move", "wheel", "key", "text"]
    x: float | None = Field(default=None, ge=0, le=1366)
    y: float | None = Field(default=None, ge=0, le=768)
    delta_x: float | None = Field(default=None, ge=-5000, le=5000)
    delta_y: float | None = Field(default=None, ge=-5000, le=5000)
    key: str | None = Field(default=None, min_length=1, max_length=64)
    text: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_event_fields(self) -> ReauthInputRequest:
        if self.type in {"click", "move"} and (self.x is None or self.y is None):
            raise ValueError(f"{self.type} requires x and y")
        if self.type == "key" and self.key is None:
            raise ValueError("key requires a key value")
        if self.type == "text" and self.text is None:
            raise ValueError("text requires a text value")
        return self


def _get_db(request: Request) -> Database:
    from app.api.dependencies.database import get_session_manager

    return get_session_manager(request)


def _get_repo(request: Request) -> AiBackupRepository:
    from app.adapters.ai_backup.repository import AiBackupRepository

    return AiBackupRepository(_get_db(request))


def _get_app_config(request: Request) -> AppConfig:
    from app.di.api import resolve_api_runtime

    return resolve_api_runtime(request).cfg


def _get_reauth_coordinator(request: Request) -> Any:
    coordinator = getattr(request.app.state, "ai_backup_reauth_coordinator", None)
    if coordinator is None:
        raise HTTPException(status_code=503, detail="AI backup re-authorization is unavailable")
    return coordinator


def get_ai_backup_owner(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Allow session-secret writes only for the configured deployment owner."""
    owner_id = next(iter(_get_app_config(request).telegram.allowed_user_ids), None)
    if owner_id is None or user["user_id"] != owner_id:
        raise HTTPException(status_code=403, detail="AI account backup is owner-only")
    return user


def _to_item(row: AiAccountBackup) -> AiBackupItem:
    backup_status = row.status.value if hasattr(row.status, "value") else str(row.status)
    authorization_status = (
        row.authorization_status.value
        if hasattr(row.authorization_status, "value")
        else str(row.authorization_status)
    )
    legacy_status = "auth_expired" if authorization_status == "expired" else backup_status
    return AiBackupItem(
        service=row.service.value if hasattr(row.service, "value") else str(row.service),
        status=legacy_status,
        backup_status=backup_status,
        authorization_status=authorization_status,
        authorization_checked_at=row.authorization_checked_at,
        last_backed_up_at=row.last_backed_up_at,
        last_attempt_at=row.last_attempt_at,
        consecutive_failures=row.consecutive_failures,
        last_error=row.last_error,
        last_error_category=row.last_error_category,
        counts=row.counts_json,
    )


def _to_reauth_flow(snapshot: Any) -> AiBackupReauthFlow:
    return AiBackupReauthFlow(
        id=snapshot.id,
        service=snapshot.service.value,
        state=snapshot.state.value,
        created_at=snapshot.created_at,
        expires_at=snapshot.expires_at,
        error=snapshot.error,
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


@router.post(
    "/{service}/reauth",
    response_model=AiBackupReauthFlow,
    status_code=201,
    responses={403: {"description": "Owner permissions required"}},
)
async def start_reauth(
    service: AiBackupService,
    user: dict[str, Any] = Depends(get_ai_backup_owner),
    coordinator: Any = Depends(_get_reauth_coordinator),
) -> AiBackupReauthFlow:
    """Start a short-lived interactive login in the private CloakBrowser."""
    try:
        snapshot = await coordinator.start(user["user_id"], service)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_reauth_flow(snapshot)


@router.get("/{service}/reauth/{flow_id}", response_model=AiBackupReauthFlow)
async def get_reauth(
    service: AiBackupService,
    flow_id: str,
    user: dict[str, Any] = Depends(get_ai_backup_owner),
    coordinator: Any = Depends(_get_reauth_coordinator),
) -> AiBackupReauthFlow:
    """Return progress without exposing cookies or browser endpoint details."""
    try:
        snapshot = await coordinator.get(user["user_id"], flow_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Re-authorization flow not found") from exc
    if snapshot.service != service:
        raise HTTPException(status_code=404, detail="Re-authorization flow not found")
    return _to_reauth_flow(snapshot)


@router.get(
    "/{service}/reauth/{flow_id}/frame",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def get_reauth_frame(
    service: AiBackupService,
    flow_id: str,
    user: dict[str, Any] = Depends(get_ai_backup_owner),
    coordinator: Any = Depends(_get_reauth_coordinator),
) -> Response:
    """Capture the current private browser frame for the owning user only."""
    try:
        snapshot = await coordinator.get(user["user_id"], flow_id)
        if snapshot.service != service:
            raise KeyError(flow_id)
        frame = await coordinator.capture_frame(user["user_id"], flow_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Re-authorization flow not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, private", "Pragma": "no-cache"},
    )


@router.post("/{service}/reauth/{flow_id}/input", status_code=204)
async def send_reauth_input(
    service: AiBackupService,
    flow_id: str,
    body: ReauthInputRequest,
    user: dict[str, Any] = Depends(get_ai_backup_owner),
    coordinator: Any = Depends(_get_reauth_coordinator),
) -> None:
    """Forward one bounded mouse/keyboard event; input is never logged or stored."""
    from app.adapters.ai_backup.reauth import ReauthInputEvent

    try:
        snapshot = await coordinator.get(user["user_id"], flow_id)
        if snapshot.service != service:
            raise KeyError(flow_id)
        await coordinator.send_input(
            user["user_id"], flow_id, ReauthInputEvent(**body.model_dump())
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Re-authorization flow not found") from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{service}/reauth/{flow_id}", status_code=204)
async def cancel_reauth(
    service: AiBackupService,
    flow_id: str,
    user: dict[str, Any] = Depends(get_ai_backup_owner),
    coordinator: Any = Depends(_get_reauth_coordinator),
) -> None:
    """Close an active interactive login flow."""
    try:
        snapshot = await coordinator.get(user["user_id"], flow_id)
        if snapshot.service != service:
            raise KeyError(flow_id)
        await coordinator.cancel(user["user_id"], flow_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Re-authorization flow not found") from exc


@router.post(
    "/{service}/session",
    status_code=204,
    responses={403: {"description": "Owner permissions required"}},
)
async def ingest_session(
    service: AiBackupService,
    body: SessionIngestRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_ai_backup_owner),
) -> None:
    """Persist a Playwright browser session for (user, service) — Mode A ingest.

    On success: 204. On bad shape: 400. The storage_state is never echoed back.
    Marks the session unverified so the next scheduled run verifies it.
    """
    from app.adapters.ai_backup.session_store import (
        AiBackupSessionStore,
        validate_storage_state,
    )

    user_id: int = user["user_id"]
    try:
        validate_storage_state(service, body.storage_state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db = _get_db(request)
    await AiBackupSessionStore(db).save(user_id, service, body.storage_state)

    # Do not report the session as valid until the provider accepts it. Keeping
    # last_backed_up_at untouched also preserves the full outage window.
    await _get_repo(request).mark_authorization_unverified(user_id, service)


@router.delete(
    "/{service}/session",
    status_code=204,
    responses={403: {"description": "Owner permissions required"}},
)
async def revoke_session(
    service: AiBackupService,
    request: Request,
    user: dict[str, Any] = Depends(get_ai_backup_owner),
) -> None:
    """Delete a stored provider session and mark authorization missing.

    The operation is owner-only and idempotent. It revokes Ratatoskr's local
    ability to use the session; it does not sign the account out at the provider.
    """
    from app.adapters.ai_backup.session_store import AiBackupSessionStore

    user_id: int = user["user_id"]
    db = _get_db(request)
    await AiBackupSessionStore(db).delete(user_id, service)
    await _get_repo(request).mark_authorization_missing(user_id, service)
