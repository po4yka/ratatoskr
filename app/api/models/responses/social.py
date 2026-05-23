"""Social account API response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.api.models.responses.common import SuccessResponse


class SocialProviderCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider: str
    supports_single_url_lookup: bool = Field(alias="supportsSingleUrlLookup")
    supports_owned_media_lookup: bool = Field(alias="supportsOwnedMediaLookup")
    supports_public_media_lookup: bool = Field(alias="supportsPublicMediaLookup")
    supports_timeline_ingestion: bool = Field(alias="supportsTimelineIngestion")
    supports_refresh_tokens: bool = Field(alias="supportsRefreshTokens")
    supported_scopes: list[str] = Field(alias="supportedScopes")
    unsupported_notes: list[str] = Field(alias="unsupportedNotes")


class SocialConnectionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider: str
    status: str
    provider_username: str | None = Field(default=None, alias="providerUsername")
    scopes: list[str] | None = None
    expires_at: str | None = Field(default=None, alias="expiresAt")
    last_used_at: str | None = Field(default=None, alias="lastUsedAt")
    created_at: str | None = Field(default=None, alias="createdAt")
    updated_at: str | None = Field(default=None, alias="updatedAt")
    connected: bool | None = None
    auth_type: str | None = Field(default=None, alias="authType")
    provider_user_id: str | None = Field(default=None, alias="providerUserId")
    token_scopes: list[str] | None = Field(default=None, alias="tokenScopes")
    access_token_expires_at: str | None = Field(default=None, alias="accessTokenExpiresAt")
    refresh_token_expires_at: str | None = Field(default=None, alias="refreshTokenExpiresAt")
    connected_at: str | None = Field(default=None, alias="connectedAt")
    metadata: dict[str, Any] | None = None
    capabilities: SocialProviderCapabilitiesResponse


class SocialConnectionsResponse(BaseModel):
    connections: list[SocialConnectionResponse]


class SocialConnectUrlResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider: str
    connect_url: str = Field(alias="connectUrl")
    state: str
    scopes: list[str]
    redirect_uri: str = Field(alias="redirectUri")
    expires_at: str = Field(alias="expiresAt")


class SocialCallbackResponse(BaseModel):
    connection: SocialConnectionResponse


class SocialDisconnectResponse(BaseModel):
    provider: str
    disconnected: bool


class SocialConnectionsSuccessResponse(SuccessResponse):
    data: SocialConnectionsResponse


class SocialConnectUrlSuccessResponse(SuccessResponse):
    data: SocialConnectUrlResponse


class SocialCallbackSuccessResponse(SuccessResponse):
    data: SocialCallbackResponse


class SocialDisconnectSuccessResponse(SuccessResponse):
    data: SocialDisconnectResponse
