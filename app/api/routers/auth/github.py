"""GitHub PAT / OAuth authentication endpoints.

US-018: POST /v1/auth/github/pat          — submit a Personal Access Token
US-019: POST /v1/auth/github/device/start — initiate OAuth Device Flow
US-019: POST /v1/auth/github/device/poll  — poll for OAuth Device Flow token
US-020: GET  /v1/auth/github/status       — query integration status
US-021: DELETE /v1/auth/github            — revoke integration
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime  # noqa: TC003
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.routers.auth.dependencies import get_current_user
from app.application.exceptions.github import InsufficientScopeError, InvalidGitHubTokenError
from app.application.ports.github_integration import GitHubAuthMethod
from app.application.use_cases.manage_github_integration import (
    ManageGitHubIntegrationUseCase,
)

router = APIRouter(prefix="/v1/auth/github", tags=["auth-github"])

_GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_DEVICE_KEY_PREFIX = "gh:device"
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------


def _get_use_case(request: Request) -> ManageGitHubIntegrationUseCase:
    """Resolve ManageGitHubIntegrationUseCase from the API runtime database."""
    from app.adapters.github.github_api_client import GitHubAPIClient
    from app.api.dependencies.database import get_session_manager
    from app.infrastructure.persistence.repositories.github_integration_repository import (
        GitHubIntegrationRepository,
    )

    db = get_session_manager(request)
    repository = GitHubIntegrationRepository(db)
    return ManageGitHubIntegrationUseCase(
        repository=repository,
        gateway_factory=GitHubAPIClient,
    )


def _get_correlation_id(request: Request) -> str:
    """Extract the correlation ID injected by correlation_id_middleware."""
    return getattr(request.state, "correlation_id", None) or str(uuid.uuid4())


async def _get_redis_or_503(request: Request) -> Any:
    """Return app-wide Redis client or raise HTTP 503 when Redis is not available."""
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "redis_not_configured",
                "hint": (
                    "OAuth Device Flow requires Redis. "
                    "Set REDIS_URL / REDIS_ENABLED=true, or use POST /v1/auth/github/pat instead."
                ),
            },
        )
    return redis


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class PATSubmitRequest(BaseModel):
    token: str = Field(..., min_length=10, max_length=200)


class PATSubmitResponse(BaseModel):
    login: str
    github_user_id: int
    auth_method: str
    status: str
    scope_warnings: list[str] | None = None


class GitHubStatusResponse(BaseModel):
    is_connected: bool
    auth_method: str | None
    github_login: str | None
    github_user_id: int | None
    status: str | None
    last_synced_at: datetime | None
    repo_count: int


class GitHubSyncResponse(BaseModel):
    status: Literal["queued"]


class DeviceFlowStartResponse(BaseModel):
    user_code: str
    verification_uri: str
    device_code: str
    interval: int
    expires_in: int


class DeviceFlowPollRequest(BaseModel):
    device_code: str = Field(..., min_length=20, max_length=200)


class DeviceFlowPollResponse(BaseModel):
    status: Literal["pending", "slow_down", "expired", "ok", "denied"]
    login: str | None = None
    github_user_id: int | None = None
    auth_method: str | None = None
    integration_status: str | None = None
    scope_warnings: list[str] | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/pat", response_model=PATSubmitResponse, status_code=200)
async def submit_pat(
    body: PATSubmitRequest,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: ManageGitHubIntegrationUseCase = Depends(_get_use_case),
    correlation_id: str = Depends(_get_correlation_id),
) -> PATSubmitResponse:
    """Store and validate a GitHub Personal Access Token."""
    try:
        integration, scope_warnings = await use_case.validate_and_store(
            body.token,
            GitHubAuthMethod.PAT,
            user["user_id"],
            correlation_id=correlation_id,
        )
    except InsufficientScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except InvalidGitHubTokenError as exc:
        raise HTTPException(status_code=400, detail="Invalid or revoked GitHub token") from exc
    return PATSubmitResponse(
        login=integration.github_login,
        github_user_id=integration.github_user_id,
        auth_method="pat",
        status="active",
        scope_warnings=scope_warnings or None,
    )


@router.get("/status", response_model=GitHubStatusResponse)
async def get_status(
    user: dict[str, Any] = Depends(get_current_user),
    use_case: ManageGitHubIntegrationUseCase = Depends(_get_use_case),
) -> GitHubStatusResponse:
    """Return the current GitHub integration status for the authenticated user."""
    s = await use_case.get_status(user["user_id"])
    return GitHubStatusResponse(
        is_connected=s.is_connected,
        auth_method=s.auth_method.value if s.auth_method else None,
        github_login=s.github_login,
        github_user_id=s.github_user_id,
        status=s.status.value if s.status else None,
        last_synced_at=s.last_synced_at,
        repo_count=s.repo_count,
    )


@router.post("/sync", response_model=GitHubSyncResponse, status_code=202)
async def trigger_sync(
    user: dict[str, Any] = Depends(get_current_user),
    use_case: ManageGitHubIntegrationUseCase = Depends(_get_use_case),
) -> GitHubSyncResponse:
    """Trigger a best-effort sync for the authenticated user's GitHub integration."""
    status = await use_case.get_status(user["user_id"])
    if not status.is_connected:
        raise HTTPException(status_code=404, detail="GitHub integration not found")
    task = asyncio.create_task(_run_user_sync(user["user_id"]))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return GitHubSyncResponse(status="queued")


@router.delete("", status_code=204)
async def revoke(
    user: dict[str, Any] = Depends(get_current_user),
    use_case: ManageGitHubIntegrationUseCase = Depends(_get_use_case),
) -> None:
    """Revoke (delete) the GitHub integration for the authenticated user."""
    await use_case.revoke(user["user_id"])


@router.post("/device/start", response_model=DeviceFlowStartResponse, status_code=200)
async def device_flow_start(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    redis: Any = Depends(_get_redis_or_503),
) -> DeviceFlowStartResponse:
    """Initiate GitHub OAuth Device Flow.

    POSTs to GitHub to get a device_code and user_code, stores
    ``gh:device:{device_code}`` in Redis (bound to the requesting user_id),
    and returns the GitHub response verbatim so the client can display the
    user_code and verification_uri.

    Raises HTTP 503 when GITHUB_OAUTH_APP_CLIENT_ID is unset or Redis is
    unavailable.
    """
    from app.config.settings import load_config

    cfg = load_config(allow_stub_telegram=True)
    client_id = cfg.github.oauth_app_client_id
    if client_id is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "oauth_not_configured",
                "hint": (
                    "Set GITHUB_OAUTH_APP_CLIENT_ID and register an OAuth App at "
                    "https://github.com/settings/applications/new"
                ),
            },
        )

    async with httpx.AsyncClient() as client:
        gh_resp = await client.post(
            _GITHUB_DEVICE_CODE_URL,
            data={"client_id": client_id, "scope": "read:user repo"},
            headers={"Accept": "application/json"},
            timeout=30.0,
        )
    gh_resp.raise_for_status()
    data: dict[str, Any] = gh_resp.json()

    device_code: str = data["device_code"]
    expires_in: int = int(data.get("expires_in", 900))
    interval: int = int(data.get("interval", 5))

    redis_key = f"{_DEVICE_KEY_PREFIX}:{device_code}"
    state = {
        "user_id": user["user_id"],
        "expires_at": int(time.time()) + expires_in,
        "last_poll_at": 0,
        "interval": interval,
    }
    await redis.set(redis_key, json.dumps(state), ex=expires_in)

    return DeviceFlowStartResponse(
        user_code=data["user_code"],
        verification_uri=data["verification_uri"],
        device_code=device_code,
        interval=interval,
        expires_in=expires_in,
    )


async def _run_user_sync(user_id: int) -> None:
    from sqlalchemy import select

    from app.api.dependencies.database import get_session_manager
    from app.config.settings import load_config
    from app.db.models.repository import GitHubIntegrationStatus, UserGitHubIntegration
    from app.tasks.github_sync import _sync_all

    cfg = load_config(allow_stub_telegram=True)
    db = get_session_manager()
    async with db.session() as session:
        integration = await session.scalar(
            select(UserGitHubIntegration).where(
                UserGitHubIntegration.user_id == user_id,
                UserGitHubIntegration.status == GitHubIntegrationStatus.ACTIVE,
            )
        )
    if integration is None:
        return
    await _sync_all(
        [integration],
        cfg=cfg,
        db=db,
        bot=None,
        correlation_id=f"github-sync-manual-{uuid.uuid4()}",
    )


@router.post("/device/poll", response_model=DeviceFlowPollResponse, status_code=200)
async def device_flow_poll(
    body: DeviceFlowPollRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    redis: Any = Depends(_get_redis_or_503),
    use_case: ManageGitHubIntegrationUseCase = Depends(_get_use_case),
    correlation_id: str = Depends(_get_correlation_id),
) -> DeviceFlowPollResponse:
    """Poll GitHub for the Device Flow access token.

    CSRF protection: the device_code Redis entry is bound to the user_id that
    called /device/start — a different JWT user gets status='expired'.

    Server-side rate-limit: if ``now - last_poll_at < interval - 1`` we return
    'slow_down' without touching GitHub.

    On success the access token is persisted via ManageGitHubIntegrationUseCase
    and the Redis entry is deleted.
    """
    from app.config.settings import load_config

    cfg = load_config(allow_stub_telegram=True)
    if cfg.github.oauth_app_client_id is None or cfg.github.oauth_app_client_secret is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "oauth_not_configured",
                "hint": "Set GITHUB_OAUTH_APP_CLIENT_ID and GITHUB_OAUTH_APP_CLIENT_SECRET",
            },
        )

    redis_key = f"{_DEVICE_KEY_PREFIX}:{body.device_code}"
    raw = await redis.get(redis_key)
    if raw is None:
        return DeviceFlowPollResponse(status="expired")

    state: dict[str, Any] = json.loads(raw)

    # CSRF: device_code must belong to the requesting user
    if int(state["user_id"]) != int(user["user_id"]):
        return DeviceFlowPollResponse(status="expired")

    # Server-side rate-limit: respect the interval negotiated with GitHub
    now = int(time.time())
    last_poll_at: int = int(state.get("last_poll_at", 0))
    stored_interval: int = int(state.get("interval", 5))
    if last_poll_at > 0 and (now - last_poll_at) < (stored_interval - 1):
        return DeviceFlowPollResponse(status="slow_down")

    # Update last_poll_at in Redis before hitting GitHub
    state["last_poll_at"] = now
    ttl_remaining = max(int(state["expires_at"]) - now, 1)
    await redis.set(redis_key, json.dumps(state), ex=ttl_remaining)

    async with httpx.AsyncClient() as client:
        gh_resp = await client.post(
            _GITHUB_TOKEN_URL,
            data={
                "client_id": cfg.github.oauth_app_client_id,
                "client_secret": cfg.github.oauth_app_client_secret.get_secret_value(),
                "device_code": body.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
            timeout=30.0,
        )
    gh_resp.raise_for_status()
    data: dict[str, Any] = gh_resp.json()

    error = data.get("error")

    if error == "authorization_pending":
        return DeviceFlowPollResponse(status="pending")

    if error == "slow_down":
        # GitHub wants a longer interval — bump and persist it
        new_interval = stored_interval + 5
        state["interval"] = new_interval
        await redis.set(redis_key, json.dumps(state), ex=ttl_remaining)
        return DeviceFlowPollResponse(status="slow_down")

    if error == "expired_token":
        await redis.delete(redis_key)
        return DeviceFlowPollResponse(status="expired")

    if error == "access_denied":
        await redis.delete(redis_key)
        return DeviceFlowPollResponse(status="denied")

    if error:
        # Unknown error — treat as expired to avoid polling loops
        await redis.delete(redis_key)
        return DeviceFlowPollResponse(status="expired")

    # --- Success: exchange token for integration record ---
    access_token: str = data["access_token"]
    await redis.delete(redis_key)

    try:
        integration, scope_warnings = await use_case.validate_and_store(
            access_token,
            GitHubAuthMethod.OAUTH_DEVICE,
            user["user_id"],
            correlation_id=correlation_id,
        )
    except InsufficientScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except InvalidGitHubTokenError as exc:
        raise HTTPException(status_code=400, detail="Invalid or revoked GitHub token") from exc

    return DeviceFlowPollResponse(
        status="ok",
        login=integration.github_login,
        github_user_id=integration.github_user_id,
        auth_method="oauth_device",
        integration_status="active",
        scope_warnings=scope_warnings or None,
    )
