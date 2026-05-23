from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace

import httpx
import pytest
from cryptography.fernet import Fernet

from app.adapters.twitter.api_extractor import XApiPostExtractor
from app.application.dto.social_auth import OAuthTokenResult
from app.application.ports.social_connections import (
    SocialConnectionRecord,
    SocialConnectionUpdate,
    SocialFetchAttemptCreate,
)
from app.core.time_utils import UTC
from app.security.secret_crypto import decrypt_secret, encrypt_secret, reset_secret_key_cache

_ACCESS_TOKEN = "old-access"
_REFRESH_TOKEN = "old-refresh"
_NEW_ACCESS_TOKEN = "new-access"
_NEW_REFRESH_TOKEN = "new-refresh"
_AUTHORIZATION_HEADER = "Authorization"


class FakeSocialConnectionRepository:
    def __init__(self, connection: SocialConnectionRecord | None) -> None:
        self.connection = connection
        self.attempts: list[SocialFetchAttemptCreate] = []
        self.updates: list[SocialConnectionUpdate] = []

    async def get_by_user_and_provider(
        self, user_id: int, provider: str
    ) -> SocialConnectionRecord | None:
        if (
            self.connection
            and self.connection.user_id == user_id
            and self.connection.provider == provider
        ):
            return self.connection
        return None

    async def update_connection(
        self, user_id: int, provider: str, update: SocialConnectionUpdate
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
            status=update.status if update.status is not None else self.connection.status,
            metadata_json=update.metadata_json
            if update.metadata_json is not None
            else self.connection.metadata_json,
        )
        return self.connection

    async def record_fetch_attempt(self, attempt: SocialFetchAttemptCreate) -> None:
        self.attempts.append(attempt)


class FakeXClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.refreshes: list[str] = []
        self.post_tokens: list[str] = []

    async def get_post_by_id(self, *, post_id: str, access_token: str) -> httpx.Response:
        assert post_id == "123"
        self.post_tokens.append(access_token)
        return self.response

    async def refresh_access_token(
        self,
        *,
        provider: str,
        refresh_token: str,
        scopes: list[str],
        correlation_id: str | None,
    ) -> OAuthTokenResult:
        del provider, scopes, correlation_id
        self.refreshes.append(refresh_token)
        return OAuthTokenResult(
            access_token=_NEW_ACCESS_TOKEN,
            refresh_token=_NEW_REFRESH_TOKEN,
            scopes=["tweet.read", "users.read", "offline.access"],
            access_token_expires_at=(dt.datetime.now(UTC) + dt.timedelta(hours=1)).isoformat(),
        )


@pytest.fixture(autouse=True)
def _crypto_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    reset_secret_key_cache()
    yield
    reset_secret_key_cache()


def _connection(
    *, expires_at: dt.datetime | None = None, status: str = "active"
) -> SocialConnectionRecord:
    now = dt.datetime.now(UTC)
    return SocialConnectionRecord(
        id=10,
        user_id=777,
        provider="x",
        auth_type="oauth2",
        provider_user_id="x-user",
        provider_username="x_user",
        encrypted_access_token=encrypt_secret(_ACCESS_TOKEN),
        encrypted_refresh_token=encrypt_secret(_REFRESH_TOKEN),
        token_scopes=["tweet.read", "users.read", "offline.access"],
        access_token_expires_at=expires_at or now + dt.timedelta(hours=1),
        refresh_token_expires_at=None,
        last_used_at=None,
        status=status,
        metadata_json={},
        created_at=now,
        updated_at=now,
    )


def _tweet_response(
    status_code: int = 200, *, headers: dict[str, str] | None = None
) -> httpx.Response:
    payload = {
        "data": {
            "id": "123",
            "text": "Hello from X https://t.co/a",
            "author_id": "42",
            "created_at": "2026-05-23T10:00:00Z",
            "lang": "en",
            "public_metrics": {"like_count": 5, "retweet_count": 2},
            "entities": {
                "urls": [{"url": "https://t.co/a", "expanded_url": "https://example.com/a"}]
            },
            "attachments": {"media_keys": ["m1"]},
        },
        "includes": {
            "users": [{"id": "42", "name": "Example User", "username": "example"}],
            "media": [
                {
                    "media_key": "m1",
                    "type": "photo",
                    "url": "https://pbs.twimg.com/media/photo.jpg",
                    "alt_text": "Chart",
                }
            ],
        },
    }
    return httpx.Response(status_code, json=payload if status_code == 200 else {}, headers=headers)


@pytest.mark.asyncio
async def test_active_connection_fetches_post_and_maps_normalized_metadata() -> None:
    repo = FakeSocialConnectionRepository(_connection())
    extractor = XApiPostExtractor(repository=repo, x_client=FakeXClient(_tweet_response()))

    result = await extractor.extract(
        url_text="https://x.com/example/status/123",
        user_id=777,
        correlation_id="cid",
        metadata={"tier_outcomes": {}},
    )

    assert result.ok is True
    assert result.content_source == "x_api"
    assert "Hello from X" in result.content_text
    assert "Example User @example" in result.content_text
    assert result.metadata["auth_strategy"]["selected_tier"] == "x_api"
    assert result.metadata["api_status"] == "ok"
    assert result.metadata["provider_resource_id"] == "123"
    assert result.metadata["tweet_media"][0]["url"] == "https://pbs.twimg.com/media/photo.jpg"
    assert repo.attempts[0].status == "succeeded"
    assert repo.attempts[0].metadata_json is not None
    assert "data" not in repo.attempts[0].metadata_json
    _assert_safe_metadata(result.metadata)
    _assert_safe_metadata(repo.attempts[0].metadata_json)


@pytest.mark.asyncio
async def test_expired_token_refreshes_before_post_lookup() -> None:
    repo = FakeSocialConnectionRepository(
        _connection(expires_at=dt.datetime.now(UTC) - dt.timedelta(seconds=1))
    )
    client = FakeXClient(_tweet_response())
    extractor = XApiPostExtractor(repository=repo, x_client=client)

    result = await extractor.extract(
        url_text="https://x.com/example/status/123",
        user_id=777,
        correlation_id="cid",
        metadata={"tier_outcomes": {}},
    )

    assert result.ok is True
    assert client.refreshes == [_REFRESH_TOKEN]
    assert client.post_tokens == [_NEW_ACCESS_TOKEN]
    assert repo.connection is not None
    assert repo.connection.encrypted_access_token is not None
    assert decrypt_secret(repo.connection.encrypted_access_token) == _NEW_ACCESS_TOKEN
    _assert_safe_metadata(result.metadata)
    _assert_safe_metadata(repo.attempts[0].metadata_json or {})


@pytest.mark.asyncio
async def test_unauthorized_marks_needs_reauth_and_records_failed_attempt() -> None:
    repo = FakeSocialConnectionRepository(_connection())
    extractor = XApiPostExtractor(repository=repo, x_client=FakeXClient(_tweet_response(401)))

    result = await extractor.extract(
        url_text="https://x.com/example/status/123",
        user_id=777,
        correlation_id="cid",
        metadata={"tier_outcomes": {}},
    )

    assert result.ok is False
    assert repo.connection is not None
    assert repo.connection.status == "needs_reauth"
    assert repo.attempts[0].status == "failed"
    assert repo.attempts[0].error_code == "unauthorized"
    assert repo.attempts[0].metadata_json is not None
    assert repo.attempts[0].metadata_json["api_status"] == "401"
    _assert_safe_metadata(result.metadata)
    _assert_safe_metadata(repo.attempts[0].metadata_json)


@pytest.mark.asyncio
async def test_rate_limited_records_reset_metadata_for_fallback() -> None:
    repo = FakeSocialConnectionRepository(_connection())
    extractor = XApiPostExtractor(
        repository=repo,
        x_client=FakeXClient(_tweet_response(429, headers={"x-rate-limit-reset": "1779519999"})),
    )

    result = await extractor.extract(
        url_text="https://x.com/example/status/123",
        user_id=777,
        correlation_id="cid",
        metadata={"tier_outcomes": {}},
    )

    assert result.ok is False
    assert result.metadata["api_status"] == "429"
    assert result.metadata["rate_limit"]["reset"] == "1779519999"
    assert repo.attempts[0].error_code == "rate_limited"
    _assert_safe_metadata(result.metadata)
    _assert_safe_metadata(repo.attempts[0].metadata_json or {})


def _assert_safe_metadata(metadata: dict[str, object]) -> None:
    rendered = json.dumps(metadata, default=str)
    for secret in (
        _ACCESS_TOKEN,
        _REFRESH_TOKEN,
        _NEW_ACCESS_TOKEN,
        _NEW_REFRESH_TOKEN,
        _AUTHORIZATION_HEADER,
    ):
        assert secret not in rendered
