"""Social account API response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.api.models.responses.common import SuccessResponse


class SocialConnectionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider: str
    connected: bool
    auth_type: str | None = Field(default=None, alias="authType")
    provider_user_id: str | None = Field(default=None, alias="providerUserId")
    provider_username: str | None = Field(default=None, alias="providerUsername")
    token_scopes: list[str] | None = Field(default=None, alias="tokenScopes")
    access_token_expires_at: str | None = Field(default=None, alias="accessTokenExpiresAt")
    refresh_token_expires_at: str | None = Field(default=None, alias="refreshTokenExpiresAt")
    status: str
    connected_at: str | None = Field(default=None, alias="connectedAt")
    updated_at: str | None = Field(default=None, alias="updatedAt")
    metadata: dict[str, Any] | None = None


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
