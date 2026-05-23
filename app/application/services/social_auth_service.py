"""Provider-neutral social account OAuth state management."""

from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.application.dto.social_auth import (
    OAuthTokenResult,
    SocialCallbackDTO,
    SocialConnectUrlDTO,
    SocialConnectionListDTO,
    SocialDisconnectDTO,
    connection_record_to_dto,
)
from app.application.ports.social_connections import (
    SUPPORTED_SOCIAL_PROVIDERS,
    SocialAuthStateCreate,
    SocialConnectionRepositoryPort,
    SocialConnectionUpdate,
    SocialConnectionUpsert,
)
from app.core.time_utils import UTC
from app.security.secret_crypto import decrypt_secret, encrypt_secret

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.application.dto.social_auth import SocialOAuthClientProtocol

DEFAULT_SOCIAL_SCOPES: dict[str, list[str]] = {
    "x": ["tweet.read", "users.read", "offline.access"],
    "instagram": ["instagram_business_basic"],
    "threads": ["threads_basic"],
}

DEFAULT_AUTHORIZATION_ENDPOINTS: dict[str, str] = {
    "x": "https://x.com/i/oauth2/authorize",
    "instagram": "https://www.instagram.com/oauth/authorize",
    "threads": "https://threads.net/oauth/authorize",
}


class SocialAuthError(ValueError):
    """Service-layer OAuth state error."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class SocialAuthConfig:
    """Runtime knobs for social OAuth state creation."""

    state_ttl_seconds: int = 600
    provider_default_scopes: Mapping[str, list[str]] | None = None
    provider_redirect_uris: Mapping[str, str | None] | None = None


class StubSocialOAuthClient:
    """OAuth client stub that can build URLs but cannot exchange codes."""

    def __init__(self, authorization_endpoint: str) -> None:
        self._authorization_endpoint = authorization_endpoint

    def build_authorization_url(
        self,
        *,
        provider: str,
        state: str,
        code_challenge: str,
        redirect_uri: str,
        scopes: list[str],
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": f"ratatoskr-{provider}-stub",
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{self._authorization_endpoint}?{urllib.parse.urlencode(params)}"

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
        del provider, code, redirect_uri, code_verifier, scopes, correlation_id
        raise SocialAuthError(
            "Social OAuth exchange client is not configured",
            code="SOCIAL_OAUTH_CLIENT_NOT_CONFIGURED",
            status_code=501,
        )

    async def refresh_access_token(
        self,
        *,
        provider: str,
        refresh_token: str,
        scopes: list[str],
        correlation_id: str | None,
    ) -> OAuthTokenResult:
        del provider, refresh_token, scopes, correlation_id
        raise SocialAuthError(
            "Social OAuth refresh client is not configured",
            code="SOCIAL_OAUTH_CLIENT_NOT_CONFIGURED",
            status_code=501,
        )


def build_stub_social_oauth_clients() -> dict[str, SocialOAuthClientProtocol]:
    """Return provider-specific stub clients for every supported provider."""
    return {
        provider: StubSocialOAuthClient(endpoint)
        for provider, endpoint in DEFAULT_AUTHORIZATION_ENDPOINTS.items()
    }


class SocialAuthService:
    """Create, validate, and consume social OAuth states."""

    def __init__(
        self,
        *,
        repository: SocialConnectionRepositoryPort,
        oauth_clients: Mapping[str, SocialOAuthClientProtocol] | None = None,
        config: SocialAuthConfig | None = None,
    ) -> None:
        self._repository = repository
        self._oauth_clients = dict(oauth_clients or build_stub_social_oauth_clients())
        self._config = config or SocialAuthConfig()
        self._default_scopes = {
            **DEFAULT_SOCIAL_SCOPES,
            **(self._config.provider_default_scopes or {}),
        }
        self._default_redirect_uris = dict(self._config.provider_redirect_uris or {})

    async def list_connections(self, user_id: int) -> SocialConnectionListDTO:
        rows = {record.provider: record for record in await self._repository.list_by_user(user_id)}
        return SocialConnectionListDTO(
            connections=[
                connection_record_to_dto(provider, rows.get(provider))
                for provider in sorted(SUPPORTED_SOCIAL_PROVIDERS)
            ]
        )

    async def create_connect_url(
        self,
        *,
        user_id: int,
        provider: str,
        redirect_uri: str | None,
        scopes: list[str] | None = None,
    ) -> SocialConnectUrlDTO:
        provider = _validate_provider(provider)
        redirect_uri = redirect_uri or self._default_redirect_uris.get(provider)
        if not redirect_uri:
            raise SocialAuthError(
                "Social OAuth redirect URI is required",
                code="SOCIAL_REDIRECT_URI_REQUIRED",
                status_code=422,
                details={"provider": provider},
            )
        requested_scopes = _normalize_scopes(scopes or self._default_scopes[provider])
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = _pkce_challenge(code_verifier)
        expires_at = datetime.now(UTC) + timedelta(seconds=self._config.state_ttl_seconds)

        await self._repository.create_auth_state(
            SocialAuthStateCreate(
                user_id=user_id,
                provider=provider,
                state_hash=_state_hash(state),
                encrypted_code_verifier=encrypt_secret(code_verifier),
                redirect_uri=redirect_uri,
                scopes=requested_scopes,
                expires_at=expires_at,
                metadata_json={"code_challenge_method": "S256"},
            )
        )

        client = self._oauth_client(provider)
        connect_url = client.build_authorization_url(
            provider=provider,
            state=state,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            scopes=requested_scopes,
        )
        return SocialConnectUrlDTO(
            provider=provider,
            connect_url=connect_url,
            state=state,
            scopes=requested_scopes,
            redirect_uri=redirect_uri,
            expires_at=expires_at.isoformat(),
        )

    async def complete_callback(
        self,
        *,
        user_id: int,
        provider: str,
        code: str,
        state: str,
        redirect_uri: str,
        correlation_id: str | None = None,
    ) -> SocialCallbackDTO:
        provider = _validate_provider(provider)
        record = await self._repository.get_auth_state(provider, _state_hash(state))
        if record is None:
            raise SocialAuthError(
                "Invalid social OAuth state",
                code="SOCIAL_AUTH_STATE_INVALID",
                status_code=400,
            )
        if record.user_id != user_id:
            raise SocialAuthError(
                "Social OAuth state does not belong to the authenticated user",
                code="SOCIAL_AUTH_STATE_FORBIDDEN",
                status_code=403,
            )
        if record.status != "pending":
            raise SocialAuthError(
                "Social OAuth state has already been used",
                code="SOCIAL_AUTH_STATE_REUSED",
                status_code=409,
            )
        now = datetime.now(UTC)
        if record.expires_at <= now:
            await self._repository.mark_auth_state_expired(record.id)
            raise SocialAuthError(
                "Social OAuth state has expired",
                code="SOCIAL_AUTH_STATE_EXPIRED",
                status_code=400,
            )
        if record.redirect_uri != redirect_uri:
            raise SocialAuthError(
                "Social OAuth redirect URI does not match the original request",
                code="SOCIAL_REDIRECT_URI_MISMATCH",
                status_code=400,
            )
        if record.encrypted_code_verifier is None:
            raise SocialAuthError(
                "Social OAuth state is missing verifier material",
                code="SOCIAL_AUTH_STATE_INVALID",
                status_code=400,
            )

        consumed = await self._repository.mark_auth_state_consumed(record.id)
        if consumed is None:
            raise SocialAuthError(
                "Social OAuth state has already been used",
                code="SOCIAL_AUTH_STATE_REUSED",
                status_code=409,
            )

        scopes = record.scopes or []
        token_result = await self._oauth_client(provider).exchange_code(
            provider=provider,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=decrypt_secret(record.encrypted_code_verifier),
            scopes=scopes,
            correlation_id=correlation_id,
        )
        connection = await self._repository.upsert_connection(
            SocialConnectionUpsert(
                user_id=user_id,
                provider=provider,
                auth_type="oauth2",
                provider_user_id=token_result.provider_user_id,
                provider_username=token_result.provider_username,
                encrypted_access_token=encrypt_secret(token_result.access_token),
                encrypted_refresh_token=encrypt_secret(token_result.refresh_token)
                if token_result.refresh_token
                else None,
                token_scopes=token_result.scopes or scopes,
                access_token_expires_at=_parse_datetime(token_result.access_token_expires_at),
                refresh_token_expires_at=_parse_datetime(token_result.refresh_token_expires_at),
                status="active",
                metadata_json=token_result.metadata_json,
            )
        )
        return SocialCallbackDTO(connection=connection_record_to_dto(provider, connection))

    async def refresh_connection(
        self,
        *,
        user_id: int,
        provider: str,
        correlation_id: str | None = None,
    ) -> SocialCallbackDTO:
        provider = _validate_provider(provider)
        record = await self._repository.get_by_user_and_provider(user_id, provider)
        if record is None:
            raise SocialAuthError(
                "Social connection was not found",
                code="SOCIAL_CONNECTION_NOT_FOUND",
                status_code=404,
            )
        if record.encrypted_refresh_token is None:
            await self._repository.update_connection(
                user_id,
                provider,
                SocialConnectionUpdate(status="needs_reauth"),
            )
            raise SocialAuthError(
                "Social connection does not have a refresh token",
                code="SOCIAL_REFRESH_TOKEN_MISSING",
                status_code=409,
            )

        try:
            token_result = await self._oauth_client(provider).refresh_access_token(
                provider=provider,
                refresh_token=decrypt_secret(record.encrypted_refresh_token),
                scopes=record.token_scopes or [],
                correlation_id=correlation_id,
            )
        except SocialAuthError:
            await self._repository.update_connection(
                user_id,
                provider,
                SocialConnectionUpdate(status="needs_reauth"),
            )
            raise

        connection = await self._repository.update_connection(
            user_id,
            provider,
            SocialConnectionUpdate(
                encrypted_access_token=encrypt_secret(token_result.access_token),
                encrypted_refresh_token=encrypt_secret(token_result.refresh_token)
                if token_result.refresh_token
                else None,
                token_scopes=token_result.scopes or record.token_scopes,
                access_token_expires_at=_parse_datetime(token_result.access_token_expires_at),
                refresh_token_expires_at=_parse_datetime(token_result.refresh_token_expires_at),
                status="active",
                metadata_json={
                    **(record.metadata_json or {}),
                    **(token_result.metadata_json or {}),
                },
            ),
        )
        if connection is None:
            raise SocialAuthError(
                "Social connection was not found",
                code="SOCIAL_CONNECTION_NOT_FOUND",
                status_code=404,
            )
        return SocialCallbackDTO(connection=connection_record_to_dto(provider, connection))

    async def disconnect(self, *, user_id: int, provider: str) -> SocialDisconnectDTO:
        provider = _validate_provider(provider)
        disconnected = await self._repository.delete_connection(user_id, provider)
        return SocialDisconnectDTO(provider=provider, disconnected=disconnected)

    def _oauth_client(self, provider: str) -> SocialOAuthClientProtocol:
        try:
            return self._oauth_clients[provider]
        except KeyError as exc:
            raise SocialAuthError(
                "Social OAuth provider client is not configured",
                code="SOCIAL_OAUTH_CLIENT_NOT_CONFIGURED",
                status_code=501,
                details={"provider": provider},
            ) from exc


def _validate_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized not in SUPPORTED_SOCIAL_PROVIDERS:
        raise SocialAuthError(
            "Unsupported social provider",
            code="SOCIAL_PROVIDER_UNSUPPORTED",
            status_code=404,
            details={"provider": provider},
        )
    return normalized


def _normalize_scopes(scopes: list[str]) -> list[str]:
    result: list[str] = []
    for scope in scopes:
        value = scope.strip()
        if value and value not in result:
            result.append(value)
    if not result:
        raise SocialAuthError(
            "At least one OAuth scope is required",
            code="SOCIAL_SCOPES_REQUIRED",
            status_code=422,
        )
    return result


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
