"""DTOs for provider-neutral social account authorization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.application.ports.social_connections import SocialConnectionRecord


@dataclass(frozen=True, slots=True)
class SocialConnectUrlDTO:
    provider: str
    connect_url: str
    state: str
    scopes: list[str]
    redirect_uri: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class SocialConnectionDTO:
    provider: str
    connected: bool
    auth_type: str | None
    provider_user_id: str | None
    provider_username: str | None
    token_scopes: list[str] | None
    access_token_expires_at: str | None
    refresh_token_expires_at: str | None
    status: str
    connected_at: str | None
    updated_at: str | None
    metadata_json: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class SocialConnectionListDTO:
    connections: list[SocialConnectionDTO]


@dataclass(frozen=True, slots=True)
class SocialCallbackDTO:
    connection: SocialConnectionDTO


@dataclass(frozen=True, slots=True)
class SocialDisconnectDTO:
    provider: str
    disconnected: bool


@dataclass(frozen=True, slots=True)
class OAuthTokenResult:
    access_token: str
    refresh_token: str | None = None
    scopes: list[str] | None = None
    access_token_expires_at: str | None = None
    refresh_token_expires_at: str | None = None
    provider_user_id: str | None = None
    provider_username: str | None = None
    metadata_json: dict[str, Any] | None = None


class SocialOAuthClientProtocol(Protocol):
    """Provider-specific OAuth operations used by SocialAuthService."""

    def build_authorization_url(
        self,
        *,
        provider: str,
        state: str,
        code_challenge: str,
        redirect_uri: str,
        scopes: list[str],
    ) -> str:
        """Return the provider authorization URL."""

    async def exchange_code(
        self,
        *,
        provider: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        scopes: list[str],
        correlation_id: str | None,
    ) -> OAuthTokenResult:
        """Exchange an authorization code for provider tokens."""

    async def refresh_access_token(
        self,
        *,
        provider: str,
        refresh_token: str,
        scopes: list[str],
        correlation_id: str | None,
    ) -> OAuthTokenResult:
        """Refresh an access token using a provider refresh token."""


def connection_record_to_dto(
    provider: str,
    record: SocialConnectionRecord | None,
) -> SocialConnectionDTO:
    """Convert a connection record to a token-free API DTO."""
    if record is None:
        return SocialConnectionDTO(
            provider=provider,
            connected=False,
            auth_type=None,
            provider_user_id=None,
            provider_username=None,
            token_scopes=None,
            access_token_expires_at=None,
            refresh_token_expires_at=None,
            status="disconnected",
            connected_at=None,
            updated_at=None,
            metadata_json=None,
        )
    safe = record.without_tokens()
    return SocialConnectionDTO(
        provider=safe.provider,
        connected=safe.status == "active",
        auth_type=safe.auth_type,
        provider_user_id=safe.provider_user_id,
        provider_username=safe.provider_username,
        token_scopes=safe.token_scopes,
        access_token_expires_at=_iso_or_none(safe.access_token_expires_at),
        refresh_token_expires_at=_iso_or_none(safe.refresh_token_expires_at),
        status=safe.status,
        connected_at=_iso_or_none(safe.created_at),
        updated_at=_iso_or_none(safe.updated_at),
        metadata_json=safe.metadata_json,
    )


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat()
