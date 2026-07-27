# ruff: noqa: TC001
"""Authentication response models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .common import AliasCompatibleResponseModel
from .user import PreferencesData


class TokenPair(AliasCompatibleResponseModel):
    access_token: str = Field(serialization_alias="accessToken", description="JWT access token")
    refresh_token: str | None = Field(
        default=None,
        serialization_alias="refreshToken",
        description="JWT refresh token (if available)",
    )
    expires_in: int = Field(serialization_alias="expiresIn")
    token_type: str = Field(default="Bearer", serialization_alias="tokenType")


class AuthTokensResponse(AliasCompatibleResponseModel):
    tokens: TokenPair
    session_id: int | None = Field(default=None, serialization_alias="sessionId")


class UserInfo(AliasCompatibleResponseModel):
    user_id: int = Field(serialization_alias="userId")
    username: str
    client_id: str = Field(serialization_alias="clientId")
    is_owner: bool = Field(default=False, serialization_alias="isOwner")
    created_at: str = Field(serialization_alias="createdAt")


class LoginData(BaseModel):
    tokens: TokenPair
    user: UserInfo
    preferences: PreferencesData
    session_id: int | None = Field(default=None, serialization_alias="sessionId")


class MessageResponse(BaseModel):
    message: str


class SuccessFlagResponse(BaseModel):
    success: bool


class SessionRevocationResponse(BaseModel):
    id: int
    revoked: bool


class LogoutAllResponse(AliasCompatibleResponseModel):
    revoked_families: int = Field(serialization_alias="revokedFamilies")
    revoked_tokens: int = Field(serialization_alias="revokedTokens")


class MagicLinkDispatchResponse(AliasCompatibleResponseModel):
    status: str
    email_sent: bool = Field(serialization_alias="emailSent")
    expires_at: str = Field(serialization_alias="expiresAt")
    magic_link: str | None = Field(default=None, serialization_alias="magicLink")
