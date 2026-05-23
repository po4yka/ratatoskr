"""Provider-neutral social account connection endpoints."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Request

from app.api.dependencies.database import get_social_connection_repository
from app.api.exceptions import APIException, ErrorCode, ErrorType
from app.api.models.requests import SocialCallbackRequest  # noqa: TC001 - FastAPI resolves body model
from app.api.models.responses import success_response
from app.api.models.responses.social import (
    SocialCallbackResponse,
    SocialCallbackSuccessResponse,
    SocialConnectionResponse,
    SocialConnectionsResponse,
    SocialConnectionsSuccessResponse,
    SocialConnectUrlResponse,
    SocialConnectUrlSuccessResponse,
    SocialDisconnectResponse,
    SocialDisconnectSuccessResponse,
)
from app.api.routers.auth import get_current_user
from app.adapters.social.meta import (
    InstagramClient,
    InstagramOAuthConfig,
    ThreadsClient,
    ThreadsOAuthConfig,
)
from app.adapters.social.x import XOAuthClient, XOAuthConfig
from app.application.services.social_auth_service import (
    DEFAULT_SOCIAL_SCOPES,
    SocialAuthConfig,
    SocialAuthError,
    SocialAuthService,
    build_stub_social_oauth_clients,
)
from app.config import load_config

router = APIRouter(prefix="/v1/social", tags=["social-auth"])


def get_social_oauth_clients() -> Any:
    """Resolve provider-specific OAuth clients.

    X is backed by a real OAuth 2.0 PKCE client; providers without concrete implementations still use token-safe stubs.
    """
    clients = build_stub_social_oauth_clients()
    cfg = load_config()
    twitter_cfg = cfg.twitter
    social_cfg = cfg.social
    clients["x"] = XOAuthClient(
        XOAuthConfig(
            client_id=twitter_cfg.x_oauth_client_id,
            client_secret=twitter_cfg.x_oauth_client_secret.get_secret_value()
            if twitter_cfg.x_oauth_client_secret is not None
            else None,
            redirect_uri=twitter_cfg.x_oauth_redirect_uri,
            scopes=twitter_cfg.x_oauth_scopes,
            api_base_url=twitter_cfg.x_api_base_url,
        )
    )
    clients["threads"] = ThreadsClient(
        ThreadsOAuthConfig(
            client_id=social_cfg.threads_client_id,
            client_secret=social_cfg.threads_client_secret.get_secret_value()
            if social_cfg.threads_client_secret is not None
            else None,
            redirect_uri=social_cfg.threads_redirect_uri,
            scopes=social_cfg.threads_scopes,
            graph_base_url=social_cfg.threads_graph_base_url,
        )
    )
    clients["instagram"] = InstagramClient(
        InstagramOAuthConfig(
            client_id=social_cfg.instagram_client_id,
            client_secret=social_cfg.instagram_client_secret.get_secret_value()
            if social_cfg.instagram_client_secret is not None
            else None,
            redirect_uri=social_cfg.instagram_redirect_uri,
            scopes=social_cfg.instagram_scopes,
            graph_base_url=social_cfg.instagram_graph_base_url,
        )
    )
    return clients


def _get_social_auth_service(
    repository: Any = Depends(get_social_connection_repository),
    oauth_clients: Any = Depends(get_social_oauth_clients),
) -> SocialAuthService:
    cfg = load_config()
    twitter_cfg = cfg.twitter
    social_cfg = cfg.social
    return SocialAuthService(
        repository=repository,
        oauth_clients=oauth_clients,
        config=SocialAuthConfig(
            provider_default_scopes={
                **DEFAULT_SOCIAL_SCOPES,
                "x": twitter_cfg.x_oauth_scopes,
                "threads": social_cfg.threads_scopes,
                "instagram": social_cfg.instagram_scopes,
            },
            provider_redirect_uris={
                "x": twitter_cfg.x_oauth_redirect_uri,
                "threads": social_cfg.threads_redirect_uri,
                "instagram": social_cfg.instagram_redirect_uri,
            },
        ),
    )


def _correlation_id(request: Request) -> str | None:
    return cast("str | None", getattr(request.state, "correlation_id", None))


def _connection_response(dto: Any) -> SocialConnectionResponse:
    return SocialConnectionResponse(
        provider=dto.provider,
        status=dto.status,
        providerUsername=dto.provider_username,
        scopes=dto.token_scopes,
        expiresAt=dto.access_token_expires_at,
        lastUsedAt=_last_used_at(dto),
        createdAt=dto.connected_at,
        updatedAt=dto.updated_at,
        connected=dto.connected,
        authType=dto.auth_type,
        providerUserId=dto.provider_user_id,
        tokenScopes=dto.token_scopes,
        accessTokenExpiresAt=dto.access_token_expires_at,
        refreshTokenExpiresAt=dto.refresh_token_expires_at,
        connectedAt=dto.connected_at,
        metadata=_safe_connection_metadata(dto.metadata_json),
    )


def _last_used_at(dto: Any) -> str | None:
    if getattr(dto, "last_used_at", None):
        return cast("str", dto.last_used_at)
    metadata = dto.metadata_json if isinstance(dto.metadata_json, dict) else {}
    value = metadata.get("last_used_at")
    return value if isinstance(value, str) else None


def _safe_connection_metadata(metadata: Any) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    allowed = {
        "account_type",
        "instagram_account",
        "provider_account",
        "threads_account",
        "x_account",
    }
    return {key: metadata[key] for key in allowed if key in metadata} or None


def _raise_api_error(exc: SocialAuthError) -> None:
    if exc.status_code == 403:
        error_code = ErrorCode.FORBIDDEN
        error_type = ErrorType.AUTHORIZATION
    elif exc.status_code == 404:
        error_code = ErrorCode.NOT_FOUND
        error_type = ErrorType.NOT_FOUND
    elif exc.status_code == 409:
        error_code = ErrorCode.CONFLICT
        error_type = ErrorType.CONFLICT
    elif exc.status_code >= 500:
        error_code = ErrorCode.FEATURE_DISABLED
        error_type = ErrorType.CONFIGURATION
    else:
        error_code = ErrorCode.VALIDATION_ERROR
        error_type = ErrorType.VALIDATION
    raise APIException(
        message=exc.message,
        error_code=error_code,
        status_code=exc.status_code,
        error_type=error_type,
        retryable=False,
        details={"reason_code": exc.code, **exc.details},
    ) from exc


@router.get("/connections", response_model=SocialConnectionsSuccessResponse)
async def list_social_connections(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    service: SocialAuthService = Depends(_get_social_auth_service),
) -> dict[str, Any]:
    """List social account connection statuses for the authenticated user."""
    result = await service.list_connections(user["user_id"])
    return success_response(
        SocialConnectionsResponse(
            connections=[_connection_response(connection) for connection in result.connections]
        ),
        correlation_id=_correlation_id(request),
    )


@router.get("/{provider}/connect-url", response_model=SocialConnectUrlSuccessResponse)
async def get_social_connect_url(
    provider: str,
    request: Request,
    redirect_uri: str | None = Query(
        default=None, alias="redirectUri", min_length=1, max_length=1000
    ),
    scopes: list[str] | None = Query(default=None),
    user: dict[str, Any] = Depends(get_current_user),
    service: SocialAuthService = Depends(_get_social_auth_service),
) -> dict[str, Any]:
    """Create a provider OAuth state and return the provider authorization URL."""
    try:
        result = await service.create_connect_url(
            user_id=user["user_id"],
            provider=provider,
            redirect_uri=redirect_uri,
            scopes=scopes,
        )
    except SocialAuthError as exc:
        _raise_api_error(exc)
    return success_response(
        SocialConnectUrlResponse(
            provider=result.provider,
            connectUrl=result.connect_url,
            state=result.state,
            scopes=result.scopes,
            redirectUri=result.redirect_uri,
            expiresAt=result.expires_at,
        ),
        correlation_id=_correlation_id(request),
    )


@router.post("/{provider}/callback", response_model=SocialCallbackSuccessResponse)
async def complete_social_callback(
    provider: str,
    body: SocialCallbackRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    service: SocialAuthService = Depends(_get_social_auth_service),
) -> dict[str, Any]:
    """Validate a social OAuth callback and store encrypted provider credentials."""
    try:
        result = await service.complete_callback(
            user_id=user["user_id"],
            provider=provider,
            code=body.code,
            state=body.state,
            redirect_uri=body.redirect_uri,
            correlation_id=_correlation_id(request),
        )
    except SocialAuthError as exc:
        _raise_api_error(exc)
    return success_response(
        SocialCallbackResponse(connection=_connection_response(result.connection)),
        correlation_id=_correlation_id(request),
    )


@router.delete("/{provider}", response_model=SocialDisconnectSuccessResponse)
async def disconnect_social_provider(
    provider: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    service: SocialAuthService = Depends(_get_social_auth_service),
) -> dict[str, Any]:
    """Disconnect a social provider for the authenticated user."""
    try:
        result = await service.disconnect(user_id=user["user_id"], provider=provider)
    except SocialAuthError as exc:
        _raise_api_error(exc)
    return success_response(
        SocialDisconnectResponse(provider=result.provider, disconnected=result.disconnected),
        correlation_id=_correlation_id(request),
    )
