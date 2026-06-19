"""
Pydantic models for authentication API.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TelegramLoginRequest(BaseModel):
    """Request body for Telegram login."""

    model_config = ConfigDict(populate_by_name=True)

    telegram_user_id: int = Field(..., alias="id")
    auth_hash: str = Field(..., alias="hash")
    auth_date: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    photo_url: str | None = None
    client_id: str = Field(
        ...,
        description="Client application ID (e.g., 'android-app-v1.0', 'ios-app-v2.0')",
        min_length=1,
        max_length=100,
    )


class RefreshTokenRequest(BaseModel):
    """Request body for token refresh."""

    refresh_token: str | None = None


class SecretLoginRequest(BaseModel):
    """Request body for secret-key login."""

    model_config = ConfigDict(populate_by_name=True)

    user_id: int
    client_id: str = Field(..., min_length=1, max_length=100)
    secret: str = Field(..., min_length=8)
    username: str | None = None


class SecretKeyCreateRequest(BaseModel):
    """Request body to create or register a client secret."""

    user_id: int
    client_id: str = Field(..., min_length=1, max_length=100)
    label: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    expires_at: datetime | None = None
    secret: str | None = Field(
        default=None,
        description="Optional client-generated secret; if omitted, server will generate",
    )
    username: str | None = None


class SecretKeyRotateRequest(BaseModel):
    """Request body to rotate an existing client secret."""

    label: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    expires_at: datetime | None = None
    secret: str | None = Field(
        default=None,
        description="Optional client-generated secret; if omitted, server will generate",
    )


class SecretKeyRevokeRequest(BaseModel):
    """Request body to revoke an existing client secret."""

    reason: str | None = Field(default=None, max_length=200)


class ClientSecretInfo(BaseModel):
    """Safe representation of a stored client secret (no hash included)."""

    id: int
    user_id: int
    client_id: str
    status: str
    label: str | None = None
    description: str | None = None
    expires_at: str | None = None
    last_used_at: str | None = None
    failed_attempts: int
    locked_until: str | None = None
    created_at: str
    updated_at: str


class SecretKeyCreateResponse(BaseModel):
    """Payload returned when creating or rotating a secret key."""

    secret: str
    key: ClientSecretInfo


class SecretKeyActionResponse(BaseModel):
    """Payload for list/revoke actions."""

    key: ClientSecretInfo


class SecretKeyListResponse(BaseModel):
    """Payload for listing stored secrets."""

    keys: list[ClientSecretInfo]


class TelegramLinkStatus(BaseModel):
    """Link status payload."""

    linked: bool
    telegram_user_id: int | None = None
    username: str | None = None
    photo_url: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    linked_at: str | None = None
    link_nonce_expires_at: str | None = None
    link_nonce: str | None = None


class TelegramLinkBeginResponse(BaseModel):
    """Begin link payload with nonce."""

    nonce: str
    expires_at: str


class TelegramLinkCompleteRequest(TelegramLoginRequest):
    """Complete linking using Telegram login payload + nonce."""

    nonce: str


class SessionInfo(BaseModel):
    """Session information for active sessions list."""

    id: int
    client_id: str | None = Field(serialization_alias="clientId")
    device_info: str | None = Field(serialization_alias="deviceInfo")
    ip_address: str | None = Field(serialization_alias="ipAddress")
    last_used_at: str | None = Field(serialization_alias="lastUsedAt")
    created_at: str = Field(serialization_alias="createdAt")
    is_current: bool = Field(default=False, serialization_alias="isCurrent")


class CredentialsLoginRequest(BaseModel):
    """Request body for nickname/email + password login."""

    model_config = ConfigDict(populate_by_name=True)

    identifier: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Nickname or email. '@' presence routes to the email branch.",
    )
    password: str = Field(..., min_length=1, max_length=4096)
    remember_me: bool = Field(
        default=False,
        description=(
            "When True, issue a long-lived refresh token (default 30d) so the "
            "session survives reload. When False, issue a short-lived refresh "
            "(default 12h); the web client stores it in sessionStorage so it "
            "vanishes on browser close."
        ),
    )
    client_id: str = Field(..., min_length=1, max_length=100)


class AppleSignInStartRequest(BaseModel):
    """Request body for Apple Sign-In authorization URL creation."""

    client_id: str = Field(..., min_length=1, max_length=100)
    redirect_uri: str = Field(..., min_length=1, max_length=500)
    scope: str = Field(default="name email", max_length=100)


class AppleSignInStartResponse(BaseModel):
    """Apple Sign-In authorization parameters."""

    authorization_url: str = Field(serialization_alias="authorizationUrl")
    state: str
    nonce: str
    code_verifier: str = Field(serialization_alias="codeVerifier")
    code_challenge: str = Field(serialization_alias="codeChallenge")
    code_challenge_method: str = Field(default="S256", serialization_alias="codeChallengeMethod")


class AppleSignInCallbackRequest(BaseModel):
    """Request body for Apple Sign-In callback token validation."""

    id_token: str = Field(..., min_length=1)
    client_id: str = Field(..., min_length=1, max_length=100)
    nonce: str | None = Field(default=None, max_length=256)


class MagicLinkRequest(BaseModel):
    """Request body for magic-link email login."""

    email: str = Field(..., min_length=3, max_length=256)
    client_id: str = Field(..., min_length=1, max_length=100)


class MagicLinkVerifyRequest(BaseModel):
    """Query model for magic-link verification."""

    token: str = Field(..., min_length=16, max_length=256)


class ChangePasswordRequest(BaseModel):
    """Request body for owner-only password change."""

    current_password: str = Field(..., min_length=1, max_length=4096)
    new_password: str = Field(..., min_length=1, max_length=4096)
