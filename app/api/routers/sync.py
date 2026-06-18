"""Database synchronization endpoints for offline mobile support."""

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.models.requests import SyncApplyRequest, SyncSessionRequest
from app.api.models.responses import (
    DeltaSyncResponseData,
    FullSyncResponseData,
    SyncApplyResponseData,
    success_response,
)
from app.api.routers.auth import get_current_user
from app.core.logging_utils import get_logger
from app.di.api import resolve_api_runtime

logger = get_logger(__name__)
router = APIRouter()


def _get_sync_service(request: Request) -> Any:
    """Resolve the shared sync service from the API runtime."""
    return resolve_api_runtime(request).sync_service


def _build_delta_etag(
    session_id: str,
    *,
    since: int,
    limit: int,
    max_server_version: int,
) -> str:
    digest = hashlib.sha256(
        f"{session_id}:{since}:{limit}:{max_server_version}".encode()
    ).hexdigest()[:16]
    return f'W/"sync-{digest}-{max_server_version}"'


# Behavior verified by test_full_sync_uses_default_limit_when_none in tests/api/test_sync_v2_contract.py
@router.post("/sessions")
async def create_sync_session(
    body: SyncSessionRequest | None = None,
    user: dict[str, Any] = Depends(get_current_user),
    svc: Any = Depends(_get_sync_service),
) -> dict[str, Any]:
    """Create or resume a sync session."""
    session = await svc.start_session(
        user_id=user["user_id"],
        client_id=user.get("client_id"),
        limit=body.limit if body else None,
    )

    return success_response(session)


@router.get("/full")
async def full_sync(
    session_id: str = Query(..., description="Sync session identifier"),
    limit: int | None = Query(None, ge=1, le=500),
    user: dict[str, Any] = Depends(get_current_user),
    svc: Any = Depends(_get_sync_service),
) -> dict[str, Any]:
    """Fetch full sync data in bounded chunks."""
    page: FullSyncResponseData = await svc.get_full(
        session_id=session_id,
        user_id=user["user_id"],
        client_id=user.get("client_id"),
        limit=limit,
    )
    return success_response(page, pagination=page.pagination)


@router.get("/delta", response_model=None)
async def delta_sync(
    request: Request,
    response: Response,
    session_id: str = Query(..., description="Sync session identifier"),
    since: int = Query(..., ge=0, description="Last seen server_version cursor"),
    limit: int | None = Query(None, ge=1, le=500),
    user: dict[str, Any] = Depends(get_current_user),
    svc: Any = Depends(_get_sync_service),
) -> dict[str, Any] | Response:
    """Fetch delta sync (created/updated/deleted) since a cursor."""
    session_payload = await svc.validate_session(
        session_id=session_id,
        user_id=user["user_id"],
        client_id=user.get("client_id"),
    )

    max_sv = await svc.get_max_server_version(user["user_id"])
    requested_limit = limit or session_payload.get("chunk_limit")
    resolve_limit = getattr(svc, "_resolve_limit", None)
    effective_limit = int(
        resolve_limit(requested_limit)
        if callable(resolve_limit)
        else requested_limit or svc.cfg.sync.default_limit
    )
    etag = _build_delta_etag(
        session_id,
        since=since,
        limit=effective_limit,
        max_server_version=max_sv,
    )

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})

    # Normal path -- fetch delta data
    page: DeltaSyncResponseData = await svc.get_delta(
        session_id=session_id,
        user_id=user["user_id"],
        client_id=user.get("client_id"),
        since=since,
        limit=limit,
    )
    pagination = {
        "total": len(page.created) + len(page.updated) + len(page.deleted),
        "limit": limit or svc.cfg.sync.default_limit,
        "offset": 0,
        "has_more": page.has_more,
    }
    # Set ETag header on successful response
    response.headers["ETag"] = etag
    return success_response(page, pagination=pagination)


@router.post("/apply")
async def apply_changes(
    payload: SyncApplyRequest,
    user: dict[str, Any] = Depends(get_current_user),
    svc: Any = Depends(_get_sync_service),
) -> dict[str, Any]:
    """Apply client-side changes with conflict detection."""
    result: SyncApplyResponseData = await svc.apply_changes(
        session_id=payload.session_id,
        user_id=user["user_id"],
        client_id=user.get("client_id"),
        changes=payload.changes,
        idempotency_key=payload.idempotency_key,
    )
    return success_response(result)
