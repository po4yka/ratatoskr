from __future__ import annotations

import datetime as dt
import json
from collections.abc import Generator
from dataclasses import replace

import httpx
import pytest
from cryptography.fernet import Fernet

from app.adapters.meta.threads_api_extractor import ThreadsApiExtractor
from app.adapters.social.meta import ThreadsClient
from app.application.dto.social_auth import OAuthTokenResult
from app.application.ports.social_connections import (
    SocialAuthStateCreate,
    SocialAuthStateRecord,
    SocialConnectionRecord,
    SocialConnectionUpdate,
    SocialConnectionUpsert,
    SocialFetchAttemptCreate,
)
from app.core.time_utils import UTC
from app.domain.models.source import SourceKind
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

    async def list_by_user(self, user_id: int) -> list[SocialConnectionRecord]:
        if self.connection is not None and self.connection.user_id == user_id:
            return [self.connection]
        return []

    async def upsert_connection(self, connection: SocialConnectionUpsert) -> SocialConnectionRecord:
        raise AssertionError("upsert_connection is not used by these tests")

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
            refresh_token_expires_at=update.refresh_token_expires_at
            if update.refresh_token_expires_at is not None
            else self.connection.refresh_token_expires_at,
            last_used_at=None,
            status=update.status if update.status is not None else self.connection.status,
            metadata_json=update.metadata_json
            if update.metadata_json is not None
            else self.connection.metadata_json,
        )
        return self.connection

    async def record_fetch_attempt(self, attempt: SocialFetchAttemptCreate) -> None:
        self.attempts.append(attempt)

    async def delete_connection(self, user_id: int, provider: str) -> bool:
        raise AssertionError("delete_connection is not used by these tests")

    async def create_auth_state(self, state: SocialAuthStateCreate) -> SocialAuthStateRecord:
        raise AssertionError("create_auth_state is not used by these tests")

    async def get_auth_state(self, provider: str, state_hash: str) -> SocialAuthStateRecord | None:
        raise AssertionError("get_auth_state is not used by these tests")

    async def mark_auth_state_consumed(self, state_id: int) -> SocialAuthStateRecord | None:
        raise AssertionError("mark_auth_state_consumed is not used by these tests")

    async def mark_auth_state_expired(self, state_id: int) -> SocialAuthStateRecord | None:
        raise AssertionError("mark_auth_state_expired is not used by these tests")


class FakeThreadsClient(ThreadsClient):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.refreshes: list[str] = []
        self.media_tokens: list[str] = []

    async def get_media_response(self, media_id: str, *, access_token: str) -> httpx.Response:
        assert media_id == "C8abc123"
        self.media_tokens.append(access_token)
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
            scopes=["threads_basic", "threads_content_publish"],
            access_token_expires_at=(dt.datetime.now(UTC) + dt.timedelta(hours=1)).isoformat(),
        )


@pytest.fixture(autouse=True)
def _crypto_key(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    reset_secret_key_cache()
    yield
    reset_secret_key_cache()


def _connection(
    *,
    expires_at: dt.datetime | None = None,
    status: str = "active",
) -> SocialConnectionRecord:
    now = dt.datetime.now(UTC)
    return SocialConnectionRecord(
        id=10,
        user_id=777,
        provider="threads",
        auth_type="oauth2",
        provider_user_id="threads-user",
        provider_username="threads_user",
        encrypted_access_token=encrypt_secret(_ACCESS_TOKEN),
        encrypted_refresh_token=encrypt_secret(_REFRESH_TOKEN),
        token_scopes=["threads_basic", "threads_content_publish"],
        access_token_expires_at=expires_at or now + dt.timedelta(hours=1),
        refresh_token_expires_at=None,
        last_used_at=None,
        status=status,
        metadata_json={},
        created_at=now,
        updated_at=now,
    )


def _media_response(
    status_code: int = 200, *, headers: dict[str, str] | None = None
) -> httpx.Response:
    payload = {
        "id": "C8abc123",
        "media_product_type": "THREADS",
        "media_type": "IMAGE",
        "media_url": "https://cdn.threads.net/photo.jpg",
        "permalink": "https://www.threads.net/@user/post/C8abc123",
        "username": "example",
        "text": "Hello from Threads",
        "timestamp": "2026-05-23T10:00:00Z",
        "shortcode": "C8abc123",
        "quoted_post": {"username": "quoted_user", "text": "Quoted context"},
        "link_attachment_url": "https://example.com/story",
        "alt_text": "Chart",
    }
    return httpx.Response(status_code, json=payload if status_code == 200 else {}, headers=headers)


@pytest.mark.asyncio
async def test_active_connection_fetches_threads_media_and_maps_document() -> None:
    repo = FakeSocialConnectionRepository(_connection())
    extractor = ThreadsApiExtractor(
        repository=repo,
        threads_client=FakeThreadsClient(_media_response()),
    )

    result = await extractor.extract(
        url="https://www.threads.net/@user/post/C8abc123",
        user_id=777,
        request_id=99,
        dedupe_hash="dedupe",
    )

    assert result.ok is True
    assert result.content_source == "threads_api"
    assert "Hello from Threads" in result.content_text
    assert "Quoted context" in result.content_text
    assert "https://example.com/story" in result.content_text
    assert result.metadata is not None
    assert result.metadata["auth_strategy"]["selected_tier"] == "threads_api"
    assert result.metadata["api_status"] == "ok"
    assert result.metadata["provider_resource_id"] == "C8abc123"
    assert result.source_item is not None
    assert result.source_item.kind == SourceKind.THREADS_POST
    assert result.normalized_document is not None
    assert result.normalized_document.media[0].url == "https://cdn.threads.net/photo.jpg"
    assert repo.attempts[0].status == "succeeded"
    assert repo.attempts[0].metadata_json is not None
    assert "threads_media" not in repo.attempts[0].metadata_json
    _assert_safe_metadata(result.metadata or {})
    _assert_safe_metadata(repo.attempts[0].metadata_json)


@pytest.mark.asyncio
async def test_no_connection_selects_scraper_fallback_metadata_without_fetch_attempt() -> None:
    repo = FakeSocialConnectionRepository(None)
    extractor = ThreadsApiExtractor(
        repository=repo,
        threads_client=FakeThreadsClient(_media_response()),
    )

    result = await extractor.extract(
        url="https://www.threads.net/@user/post/C8abc123",
        user_id=777,
        request_id=99,
        dedupe_hash="dedupe",
    )

    assert result.ok is False
    assert result.metadata is not None
    assert result.metadata["api_status"] == "no_connection"
    assert result.metadata["auth_strategy"]["selected_tier"] == "meta_scraper_fallback"
    assert repo.attempts == []
    _assert_safe_metadata(result.metadata)


@pytest.mark.asyncio
async def test_expired_token_refreshes_before_media_lookup() -> None:
    repo = FakeSocialConnectionRepository(
        _connection(expires_at=dt.datetime.now(UTC) - dt.timedelta(seconds=1))
    )
    client = FakeThreadsClient(_media_response())
    extractor = ThreadsApiExtractor(repository=repo, threads_client=client)

    result = await extractor.extract(
        url="https://www.threads.net/@user/post/C8abc123",
        user_id=777,
        request_id=99,
        dedupe_hash="dedupe",
    )

    assert result.ok is True
    assert client.refreshes == [_REFRESH_TOKEN]
    assert client.media_tokens == [_NEW_ACCESS_TOKEN]
    assert repo.connection is not None
    assert repo.connection.encrypted_access_token is not None
    assert decrypt_secret(repo.connection.encrypted_access_token) == _NEW_ACCESS_TOKEN
    _assert_safe_metadata(result.metadata or {})
    _assert_safe_metadata(repo.attempts[0].metadata_json or {})


@pytest.mark.asyncio
async def test_unauthorized_marks_needs_reauth_and_records_failed_attempt() -> None:
    repo = FakeSocialConnectionRepository(_connection())
    extractor = ThreadsApiExtractor(
        repository=repo,
        threads_client=FakeThreadsClient(_media_response(401)),
    )

    result = await extractor.extract(
        url="https://www.threads.net/@user/post/C8abc123",
        user_id=777,
        request_id=99,
        dedupe_hash="dedupe",
    )

    assert result.ok is False
    assert repo.connection is not None
    assert repo.connection.status == "needs_reauth"
    assert repo.attempts[0].status == "failed"
    assert repo.attempts[0].error_code == "unauthorized"
    assert repo.attempts[0].metadata_json is not None
    assert repo.attempts[0].metadata_json["api_status"] == "401"
    assert result.metadata is not None
    assert result.metadata["auth_strategy"]["selected_tier"] == "meta_scraper_fallback"
    _assert_safe_metadata(result.metadata)
    _assert_safe_metadata(repo.attempts[0].metadata_json)


@pytest.mark.asyncio
async def test_rate_limited_records_reset_metadata_for_scraper_fallback() -> None:
    repo = FakeSocialConnectionRepository(_connection())
    extractor = ThreadsApiExtractor(
        repository=repo,
        threads_client=FakeThreadsClient(
            _media_response(429, headers={"x-rate-limit-reset": "1779519999"})
        ),
    )

    result = await extractor.extract(
        url="https://www.threads.net/@user/post/C8abc123",
        user_id=777,
        request_id=99,
        dedupe_hash="dedupe",
    )

    assert result.ok is False
    assert result.metadata is not None
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
