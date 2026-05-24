from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import replace
from typing import Any, cast

import pytest
from cryptography.fernet import Fernet

from app.application.dto.social_auth import OAuthTokenResult, SocialOAuthClientProtocol
from app.application.ports.social_connections import (
    SocialConnectionRecord,
    SocialConnectionRepositoryPort,
    SocialConnectionUpdate,
)
from app.application.services.social_token_service import SocialAccessTokenResolver
from app.core.time_utils import UTC
from app.security.secret_crypto import decrypt_secret, encrypt_secret, reset_secret_key_cache


class _FakeRepository:
    def __init__(self, connection: SocialConnectionRecord | None) -> None:
        self.connection = connection
        self.updates: list[SocialConnectionUpdate] = []

    async def get_by_user_and_provider(
        self,
        user_id: int,
        provider: str,
    ) -> SocialConnectionRecord | None:
        if (
            self.connection is not None
            and self.connection.user_id == user_id
            and self.connection.provider == provider
        ):
            return self.connection
        return None

    async def update_connection(
        self,
        user_id: int,
        provider: str,
        update: SocialConnectionUpdate,
    ) -> SocialConnectionRecord | None:
        assert self.connection is not None
        assert self.connection.user_id == user_id
        assert self.connection.provider == provider
        self.updates.append(update)
        self.connection = replace(
            self.connection,
            encrypted_access_token=update.encrypted_access_token
            if update.encrypted_access_token is not None
            else self.connection.encrypted_access_token,
            encrypted_refresh_token=update.encrypted_refresh_token
            if update.encrypted_refresh_token is not None
            else self.connection.encrypted_refresh_token,
            token_scopes=update.token_scopes
            if update.token_scopes is not None
            else self.connection.token_scopes,
            access_token_expires_at=update.access_token_expires_at
            if update.access_token_expires_at is not None
            else self.connection.access_token_expires_at,
            refresh_token_expires_at=update.refresh_token_expires_at
            if update.refresh_token_expires_at is not None
            else self.connection.refresh_token_expires_at,
            status=update.status if update.status is not None else self.connection.status,
            metadata_json=update.metadata_json
            if update.metadata_json is not None
            else self.connection.metadata_json,
        )
        return self.connection


class _FakeOAuthClient:
    def __init__(self, *, fail_refresh: bool = False) -> None:
        self.fail_refresh = fail_refresh
        self.refreshes: list[dict[str, Any]] = []

    async def refresh_access_token(
        self,
        *,
        provider: str,
        refresh_token: str,
        scopes: list[str],
        correlation_id: str | None,
    ) -> OAuthTokenResult:
        if self.fail_refresh:
            raise RuntimeError("refresh failed")
        self.refreshes.append(
            {
                "provider": provider,
                "refresh_token": refresh_token,
                "scopes": scopes,
                "correlation_id": correlation_id,
            }
        )
        return OAuthTokenResult(
            access_token="new-access",
            refresh_token="new-refresh",
            scopes=scopes,
            access_token_expires_at=(dt.datetime.now(UTC) + dt.timedelta(hours=1)).isoformat(),
            refresh_token_expires_at=(dt.datetime.now(UTC) + dt.timedelta(days=30)).isoformat(),
            metadata_json={"provider_account": {"id": "acct"}},
        )


@pytest.fixture(autouse=True)
def _crypto_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    reset_secret_key_cache()
    yield
    reset_secret_key_cache()


def _connection(
    *,
    status: str = "active",
    scopes: list[str] | None = None,
    expires_at: dt.datetime | None = None,
    refresh_token: str | None = "old-refresh",
) -> SocialConnectionRecord:
    now = dt.datetime.now(UTC)
    return SocialConnectionRecord(
        id=10,
        user_id=777,
        provider="x",
        auth_type="oauth2",
        provider_user_id="x-user",
        provider_username="x_user",
        encrypted_access_token=encrypt_secret("old-access"),
        encrypted_refresh_token=encrypt_secret(refresh_token)
        if refresh_token is not None
        else None,
        token_scopes=scopes or ["tweet.read", "users.read"],
        access_token_expires_at=expires_at or now + dt.timedelta(hours=1),
        refresh_token_expires_at=None,
        last_used_at=None,
        status=status,
        metadata_json={},
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_missing_connection_returns_safe_status() -> None:
    resolver = SocialAccessTokenResolver(
        repository=cast("SocialConnectionRepositoryPort", _FakeRepository(None)),
        oauth_clients={"x": cast("SocialOAuthClientProtocol", _FakeOAuthClient())},
    )

    result = await resolver.resolve(user_id=777, provider="x", required_scopes=["tweet.read"])

    assert result.ok is False
    assert result.status == "no_connection"
    assert result.access_token is None
    assert result.safe_metadata() == {"api_status": "no_connection", "auth_reason": "missing"}


@pytest.mark.asyncio
async def test_missing_scope_returns_safe_status_without_decrypting() -> None:
    resolver = SocialAccessTokenResolver(
        repository=cast(
            "SocialConnectionRepositoryPort",
            _FakeRepository(_connection(scopes=["users.read"])),
        ),
        oauth_clients={"x": cast("SocialOAuthClientProtocol", _FakeOAuthClient())},
    )

    result = await resolver.resolve(
        user_id=777,
        provider="x",
        required_scopes=["tweet.read", "users.read"],
    )

    assert result.ok is False
    assert result.status == "missing_scope"
    assert result.access_token is None
    assert result.missing_scopes == ("tweet.read",)
    assert "old-access" not in repr(result)


@pytest.mark.asyncio
async def test_expired_token_refresh_success_returns_new_decrypted_token() -> None:
    repo = _FakeRepository(_connection(expires_at=dt.datetime.now(UTC) - dt.timedelta(seconds=1)))
    client = _FakeOAuthClient()
    resolver = SocialAccessTokenResolver(
        repository=cast("SocialConnectionRepositoryPort", repo),
        oauth_clients={"x": cast("SocialOAuthClientProtocol", client)},
    )

    result = await resolver.resolve(
        user_id=777,
        provider="x",
        required_scopes=["tweet.read"],
        correlation_id="cid",
    )

    assert result.ok is True
    assert result.access_token is not None
    assert result.access_token.get_secret_value() == "new-access"
    assert "new-access" not in repr(result)
    assert client.refreshes == [
        {
            "provider": "x",
            "refresh_token": "old-refresh",
            "scopes": ["tweet.read", "users.read"],
            "correlation_id": "cid",
        }
    ]
    assert repo.connection is not None
    assert repo.connection.encrypted_access_token is not None
    assert decrypt_secret(repo.connection.encrypted_access_token) == "new-access"
    assert repo.connection.status == "active"


@pytest.mark.asyncio
async def test_refresh_failure_marks_needs_reauth_and_returns_safe_status() -> None:
    repo = _FakeRepository(_connection(expires_at=dt.datetime.now(UTC) - dt.timedelta(seconds=1)))
    resolver = SocialAccessTokenResolver(
        repository=cast("SocialConnectionRepositoryPort", repo),
        oauth_clients={"x": cast("SocialOAuthClientProtocol", _FakeOAuthClient(fail_refresh=True))},
    )

    result = await resolver.resolve(user_id=777, provider="x", required_scopes=["tweet.read"])

    assert result.ok is False
    assert result.status == "refresh_failed"
    assert result.access_token is None
    assert repo.connection is not None
    assert repo.connection.status == "needs_reauth"


@pytest.mark.asyncio
async def test_missing_refresh_token_marks_needs_reauth() -> None:
    repo = _FakeRepository(
        _connection(
            expires_at=dt.datetime.now(UTC) - dt.timedelta(seconds=1),
            refresh_token=None,
        )
    )
    resolver = SocialAccessTokenResolver(
        repository=cast("SocialConnectionRepositoryPort", repo),
        oauth_clients={"x": cast("SocialOAuthClientProtocol", _FakeOAuthClient())},
    )

    result = await resolver.resolve(user_id=777, provider="x", required_scopes=["tweet.read"])

    assert result.ok is False
    assert result.status == "refresh_failed"
    assert repo.connection is not None
    assert repo.connection.status == "needs_reauth"


@pytest.mark.asyncio
async def test_revoked_connection_is_not_refreshed() -> None:
    client = _FakeOAuthClient()
    resolver = SocialAccessTokenResolver(
        repository=cast(
            "SocialConnectionRepositoryPort",
            _FakeRepository(_connection(status="revoked")),
        ),
        oauth_clients={"x": cast("SocialOAuthClientProtocol", client)},
    )

    result = await resolver.resolve(user_id=777, provider="x", required_scopes=["tweet.read"])

    assert result.ok is False
    assert result.status == "revoked"
    assert result.access_token is None
    assert client.refreshes == []
