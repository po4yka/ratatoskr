"""Shared social access token resolution policy."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from app.application.ports.social_connections import (
    ResolvedSocialAccessToken,
    SocialAccessTokenResolution,
    SocialConnectionUpdate,
)
from app.core.time_utils import UTC
from app.observability.metrics import record_social_token_refresh
from app.security.secret_crypto import decrypt_secret, encrypt_secret

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from app.application.dto.social_auth import OAuthTokenResult, SocialOAuthClientProtocol
    from app.application.ports.social_connections import (
        SocialConnectionRecord,
        SocialConnectionRepositoryPort,
    )


class SocialAccessTokenResolver:
    """Resolve a usable provider access token from encrypted social connections."""

    def __init__(
        self,
        *,
        repository: SocialConnectionRepositoryPort,
        oauth_clients: Mapping[str, SocialOAuthClientProtocol],
    ) -> None:
        self._repository = repository
        self._oauth_clients = dict(oauth_clients)

    async def resolve(
        self,
        *,
        user_id: int | None,
        provider: str,
        required_scopes: Sequence[str] = (),
        correlation_id: str | None = None,
    ) -> SocialAccessTokenResolution:
        if user_id is None:
            return SocialAccessTokenResolution(
                provider=provider,
                status="skipped",
                reason="no_user_context",
            )

        connection = await self._repository.get_by_user_and_provider(user_id, provider)
        if connection is None:
            return SocialAccessTokenResolution(
                provider=provider,
                status="no_connection",
                reason="missing",
            )
        if connection.status != "active":
            return SocialAccessTokenResolution(
                provider=provider,
                status=connection.status,
                connection=connection,
                reason="inactive",
            )
        if connection.encrypted_access_token is None:
            return SocialAccessTokenResolution(
                provider=provider,
                status="missing_access_token",
                connection=connection,
                reason="missing_access_token",
            )

        missing_scopes = _missing_scopes(connection.token_scopes or [], required_scopes)
        if missing_scopes:
            return SocialAccessTokenResolution(
                provider=provider,
                status="missing_scope",
                connection=connection,
                reason="missing_scope",
                missing_scopes=missing_scopes,
            )

        refreshed = await self._refresh_if_needed(
            connection,
            provider=provider,
            correlation_id=correlation_id,
        )
        if refreshed.status != "active" or refreshed.encrypted_access_token is None:
            return SocialAccessTokenResolution(
                provider=provider,
                status="refresh_failed",
                connection=refreshed,
                reason="refresh_failed",
            )

        return SocialAccessTokenResolution(
            provider=provider,
            status="ok",
            access_token=ResolvedSocialAccessToken(
                decrypt_secret(refreshed.encrypted_access_token)
            ),
            connection=refreshed,
        )

    async def mark_needs_reauth(
        self,
        *,
        user_id: int,
        provider: str,
    ) -> SocialConnectionRecord | None:
        return await self._repository.update_connection(
            user_id,
            provider,
            SocialConnectionUpdate(status="needs_reauth"),
        )

    async def _refresh_if_needed(
        self,
        connection: SocialConnectionRecord,
        *,
        provider: str,
        correlation_id: str | None,
    ) -> SocialConnectionRecord:
        expires_at = connection.access_token_expires_at
        if expires_at is None or expires_at > datetime.now(UTC):
            return connection
        if connection.encrypted_refresh_token is None:
            record_social_token_refresh(provider=provider, status="missing_refresh_token")
            updated = await self.mark_needs_reauth(user_id=connection.user_id, provider=provider)
            return updated or connection

        try:
            token_result = await self._oauth_clients[provider].refresh_access_token(
                provider=provider,
                refresh_token=decrypt_secret(connection.encrypted_refresh_token),
                scopes=connection.token_scopes or [],
                correlation_id=correlation_id,
            )
        except Exception:
            record_social_token_refresh(provider=provider, status="failed")
            updated = await self.mark_needs_reauth(user_id=connection.user_id, provider=provider)
            return updated or connection

        record_social_token_refresh(provider=provider, status="succeeded")
        updated = await self._repository.update_connection(
            connection.user_id,
            provider,
            _token_result_to_update(connection, token_result),
        )
        return updated or connection


def _token_result_to_update(
    connection: SocialConnectionRecord,
    token_result: OAuthTokenResult,
) -> SocialConnectionUpdate:
    return SocialConnectionUpdate(
        encrypted_access_token=encrypt_secret(token_result.access_token),
        encrypted_refresh_token=encrypt_secret(token_result.refresh_token)
        if token_result.refresh_token
        else None,
        token_scopes=token_result.scopes or connection.token_scopes,
        access_token_expires_at=_parse_datetime(token_result.access_token_expires_at),
        refresh_token_expires_at=_parse_datetime(token_result.refresh_token_expires_at),
        status="active",
        metadata_json={**(connection.metadata_json or {}), **(token_result.metadata_json or {})},
    )


def _missing_scopes(have: Sequence[str], required: Sequence[str]) -> tuple[str, ...]:
    present = set(have)
    return tuple(scope for scope in required if scope not in present)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
