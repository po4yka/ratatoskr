"""Provider-neutral social account connection endpoints."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Request

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
    SocialProviderCapabilitiesResponse,
)
from app.application.dto.social_capabilities import get_social_provider_capabilities
from app.api.routers.auth import get_current_user
from app.application.services.social_auth_service import SocialAuthError, SocialAuthService
from app.di.api import resolve_api_runtime

router = APIRouter(prefix="/v1/social", tags=["social-auth"])


def get_social_auth_service(request: Request) -> SocialAuthService:
    """Resolve social auth service from the API runtime.

    Kept as a named dependency so tests can override the service directly.
    """
    return cast("SocialAuthService", resolve_api_runtime(request).social_auth_service)


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
        capabilities=_capabilities_response(dto.provider),
    )


def _capabilities_response(provider: str) -> SocialProviderCapabilitiesResponse:
    capabilities = get_social_provider_capabilities(provider)
    return SocialProviderCapabilitiesResponse(
        provider=capabilities.provider,
        supportsSingleUrlLookup=capabilities.supports_single_url_lookup,
        supportsOwnedMediaLookup=capabilities.supports_owned_media_lookup,
        supportsPublicMediaLookup=capabilities.supports_public_media_lookup,
        supportsTimelineIngestion=capabilities.supports_timeline_ingestion,
        supportsRefreshTokens=capabilities.supports_refresh_tokens,
        supportedScopes=list(capabilities.supported_scopes),
        unsupportedNotes=list(capabilities.unsupported_notes),
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
    service: SocialAuthService = Depends(get_social_auth_service),
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
    service: SocialAuthService = Depends(get_social_auth_service),
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
    service: SocialAuthService = Depends(get_social_auth_service),
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
    service: SocialAuthService = Depends(get_social_auth_service),
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
