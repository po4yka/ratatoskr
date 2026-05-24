from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from app.adapters.social.meta import ThreadsClient, ThreadsOAuthConfig
from app.application.ports.social_connections import (
    SocialAuthStateCreate,
    SocialAuthStateRecord,
    SocialConnectionRecord,
    SocialConnectionUpdate,
    SocialConnectionUpsert,
)
from app.application.services.social_auth_service import SocialAuthError, SocialAuthService
from app.config import clear_config_cache
from app.core.time_utils import UTC
from app.security.secret_crypto import decrypt_secret, encrypt_secret, reset_secret_key_cache

_USER_ID = 88001
_REDIRECT_URI = "https://app.example.com/social/threads/callback"


class InMemorySocialRepository:
    def __init__(self) -> None:
        self.auth_state: SocialAuthStateRecord | None = None
        self.connection: SocialConnectionRecord | None = None

    async def get_by_user_and_provider(
        self, user_id: int, provider: str
    ) -> SocialConnectionRecord | None:
        if (
            self.connection is not None
            and self.connection.user_id == user_id
            and self.connection.provider == provider
        ):
            return self.connection
        return None

    async def list_by_user(self, user_id: int) -> list[SocialConnectionRecord]:
        return (
            [self.connection]
            if self.connection is not None and self.connection.user_id == user_id
            else []
        )

    async def upsert_connection(self, connection: SocialConnectionUpsert) -> SocialConnectionRecord:
        now = dt.datetime.now(UTC)
        self.connection = SocialConnectionRecord(
            id=1,
            user_id=connection.user_id,
            provider=connection.provider,
            auth_type=connection.auth_type,
            provider_user_id=connection.provider_user_id,
            provider_username=connection.provider_username,
            encrypted_access_token=connection.encrypted_access_token,
            encrypted_refresh_token=connection.encrypted_refresh_token,
            token_scopes=connection.token_scopes,
            access_token_expires_at=connection.access_token_expires_at,
            refresh_token_expires_at=connection.refresh_token_expires_at,
            last_used_at=None,
            status=connection.status,
            metadata_json=connection.metadata_json,
            created_at=now,
            updated_at=now,
        )
        return self.connection

    async def update_connection(
        self, user_id: int, provider: str, update: SocialConnectionUpdate
    ) -> SocialConnectionRecord | None:
        if self.connection is None:
            return None
        assert self.connection.user_id == user_id
        assert self.connection.provider == provider
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

    async def delete_connection(self, user_id: int, provider: str) -> bool:
        if self.connection is None:
            return False
        if self.connection.user_id == user_id and self.connection.provider == provider:
            self.connection = None
            return True
        return False

    async def create_auth_state(self, state: SocialAuthStateCreate) -> SocialAuthStateRecord:
        self.auth_state = SocialAuthStateRecord(
            id=1,
            user_id=state.user_id,
            provider=state.provider,
            state_hash=state.state_hash,
            encrypted_code_verifier=state.encrypted_code_verifier,
            redirect_uri=state.redirect_uri,
            scopes=state.scopes,
            status="pending",
            metadata_json=state.metadata_json,
            expires_at=state.expires_at,
            consumed_at=None,
            created_at=dt.datetime.now(UTC),
        )
        return self.auth_state

    async def get_auth_state(self, provider: str, state_hash: str) -> SocialAuthStateRecord | None:
        if (
            self.auth_state is not None
            and self.auth_state.provider == provider
            and self.auth_state.state_hash == state_hash
        ):
            return self.auth_state
        return None

    async def mark_auth_state_consumed(self, state_id: int) -> SocialAuthStateRecord | None:
        if (
            self.auth_state is None
            or self.auth_state.id != state_id
            or self.auth_state.status != "pending"
        ):
            return None
        self.auth_state = replace(
            self.auth_state,
            status="consumed",
            consumed_at=dt.datetime.now(UTC),
        )
        return self.auth_state

    async def mark_auth_state_expired(self, state_id: int) -> SocialAuthStateRecord | None:
        if self.auth_state is None or self.auth_state.id != state_id:
            return None
        self.auth_state = replace(self.auth_state, status="expired")
        return self.auth_state

    async def record_fetch_attempt(self, attempt: Any) -> None:
        del attempt


@pytest.fixture(autouse=True)
def _crypto_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    reset_secret_key_cache()
    yield
    reset_secret_key_cache()


def _client() -> ThreadsClient:
    return ThreadsClient(
        ThreadsOAuthConfig(
            client_id="threads-client-id",
            client_secret="threads-client-secret",
            redirect_uri=_REDIRECT_URI,
            scopes=["threads_basic"],
        )
    )


class _BrokenAuthorizationClient:
    def build_authorization_url(
        self,
        *,
        provider: str,
        state: str,
        code_challenge: str,
        redirect_uri: str,
        scopes: list[str],
    ) -> str:
        del provider, state, code_challenge, redirect_uri, scopes
        raise RuntimeError("provider URL includes https://example.com/callback?code=raw-code")

    async def exchange_code(
        self,
        *,
        provider: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        scopes: list[str],
        correlation_id: str | None,
    ) -> Any:
        del provider, code, redirect_uri, code_verifier, scopes, correlation_id
        raise AssertionError("exchange_code should not be called")

    async def refresh_access_token(
        self,
        *,
        provider: str,
        refresh_token: str,
        scopes: list[str],
        correlation_id: str | None,
    ) -> Any:
        del provider, refresh_token, scopes, correlation_id
        raise AssertionError("refresh_access_token should not be called")


def test_authorization_url_uses_threads_auth_surface() -> None:
    url = _client().build_authorization_url(
        provider="threads",
        state="state-123",
        code_challenge="unused-pkce",
        redirect_uri=_REDIRECT_URI,
        scopes=["threads_basic"],
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert (
        f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://threads.net/oauth/authorize"
    )
    assert query["client_id"] == ["threads-client-id"]
    assert query["redirect_uri"] == [_REDIRECT_URI]
    assert query["scope"] == ["threads_basic"]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["state-123"]


def test_social_di_wires_threads_client_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import load_config
    from app.di.social import build_social_oauth_clients

    monkeypatch.setenv("THREADS_CLIENT_ID", "threads-client-id")
    monkeypatch.setenv("THREADS_CLIENT_SECRET", "threads-client-secret")
    monkeypatch.setenv("THREADS_REDIRECT_URI", _REDIRECT_URI)
    monkeypatch.setenv("THREADS_SCOPES", "threads_basic")
    clear_config_cache()
    try:
        clients = build_social_oauth_clients(load_config(allow_stub_telegram=True))
        url = clients["threads"].build_authorization_url(
            provider="threads",
            state="state-123",
            code_challenge="unused",
            redirect_uri=_REDIRECT_URI,
            scopes=["threads_basic"],
        )
    finally:
        clear_config_cache()

    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["threads-client-id"]
    assert query["scope"] == ["threads_basic"]


@pytest.mark.asyncio
async def test_social_auth_service_wraps_authorization_url_errors_without_leaking_url() -> None:
    service = SocialAuthService(
        repository=InMemorySocialRepository(),
        oauth_clients={"threads": _BrokenAuthorizationClient()},
    )

    with pytest.raises(SocialAuthError) as exc_info:
        await service.create_connect_url(
            user_id=_USER_ID,
            provider="threads",
            redirect_uri=_REDIRECT_URI,
        )

    exc = exc_info.value
    assert exc.code == "SOCIAL_AUTHORIZATION_URL_FAILED"
    assert exc.details == {"provider": "threads"}
    assert "raw-code" not in exc.message
    assert "callback" not in exc.message


@pytest.mark.asyncio
async def test_token_exchange_gets_long_lived_token_and_stores_encrypted_connection(
    respx_mock,
) -> None:
    respx_mock.post("https://graph.threads.net/oauth/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "short-token"})
    )
    respx_mock.get("https://graph.threads.net/access_token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "long-lived-token",
                "token_type": "bearer",
                "expires_in": 5184000,
            },
        )
    )
    respx_mock.get("https://graph.threads.net/v1.0/me").mock(
        return_value=httpx.Response(200, json={"id": "1789", "username": "threads_user"})
    )
    repo = InMemorySocialRepository()
    service = SocialAuthService(
        repository=repo,
        oauth_clients={"threads": _client()},
    )
    connect = await service.create_connect_url(
        user_id=_USER_ID,
        provider="threads",
        redirect_uri=_REDIRECT_URI,
    )

    callback = await service.complete_callback(
        user_id=_USER_ID,
        provider="threads",
        code="provider-code",
        state=connect.state,
        redirect_uri=_REDIRECT_URI,
        correlation_id="cid",
    )

    assert callback.connection.provider == "threads"
    assert callback.connection.provider_user_id == "1789"
    assert callback.connection.provider_username == "threads_user"
    assert repo.connection is not None
    assert repo.connection.encrypted_access_token is not None
    assert repo.connection.encrypted_refresh_token is not None
    assert decrypt_secret(repo.connection.encrypted_access_token) == "long-lived-token"
    assert decrypt_secret(repo.connection.encrypted_refresh_token) == "long-lived-token"
    assert repo.connection.status == "active"


@pytest.mark.asyncio
async def test_media_retrieval_normalizes_threads_fields(respx_mock) -> None:
    route = respx_mock.get("https://graph.threads.net/v1.0/media-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "media-1",
                "media_product_type": "THREADS",
                "media_type": "IMAGE",
                "media_url": "https://cdn.example.com/image.jpg",
                "permalink": "https://www.threads.net/@u/post/abc",
                "owner": {"id": "1789"},
                "username": "threads_user",
                "text": "hello",
                "timestamp": "2026-05-23T10:00:00+0000",
                "shortcode": "abc",
                "thumbnail_url": "https://cdn.example.com/thumb.jpg",
                "children": [{"id": "child-1"}],
                "is_quote_post": True,
                "quoted_post": {"id": "quoted"},
                "reposted_post": {"id": "reposted"},
                "alt_text": "Alt",
                "link_attachment_url": "https://example.com",
            },
        )
    )

    media = await _client().get_media("media-1", access_token="threads-token")

    assert media.to_dict() == {
        "id": "media-1",
        "media_product_type": "THREADS",
        "media_type": "IMAGE",
        "media_url": "https://cdn.example.com/image.jpg",
        "permalink": "https://www.threads.net/@u/post/abc",
        "owner": {"id": "1789"},
        "username": "threads_user",
        "text": "hello",
        "timestamp": "2026-05-23T10:00:00+0000",
        "shortcode": "abc",
        "thumbnail_url": "https://cdn.example.com/thumb.jpg",
        "children": [{"id": "child-1"}],
        "is_quote_post": True,
        "quoted_post": {"id": "quoted"},
        "reposted_post": {"id": "reposted"},
        "alt_text": "Alt",
        "link_attachment_url": "https://example.com",
    }
    query = parse_qs(route.calls[0].request.url.query.decode())
    assert query["access_token"] == ["threads-token"]
    assert "threads_content_publish" not in query["fields"][0]


@pytest.mark.asyncio
async def test_get_user_threads_supports_paging_options(respx_mock) -> None:
    route = respx_mock.get("https://graph.threads.net/v1.0/me/threads").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "media-1", "media_type": "TEXT_POST", "text": "hello"}],
                "paging": {"cursors": {"after": "cursor-after"}},
            },
        )
    )

    result = await _client().get_user_threads(
        access_token="threads-token",
        limit=25,
        after="cursor",
        since="1779510000",
    )

    assert result["data"][0]["id"] == "media-1"
    assert result["data"][0]["text"] == "hello"
    assert result["paging"] == {"cursors": {"after": "cursor-after"}}
    query = parse_qs(route.calls[0].request.url.query.decode())
    assert query["limit"] == ["25"]
    assert query["after"] == ["cursor"]
    assert query["since"] == ["1779510000"]


@pytest.mark.asyncio
async def test_refresh_failure_marks_connection_needs_reauth(respx_mock) -> None:
    respx_mock.get("https://graph.threads.net/refresh_access_token").mock(
        return_value=httpx.Response(400, json={"error": {"message": "invalid token"}})
    )
    repo = InMemorySocialRepository()
    now = dt.datetime.now(UTC)
    repo.connection = SocialConnectionRecord(
        id=1,
        user_id=_USER_ID,
        provider="threads",
        auth_type="oauth2",
        provider_user_id="1789",
        provider_username="threads_user",
        encrypted_access_token=None,
        encrypted_refresh_token=None,
        token_scopes=["threads_basic"],
        access_token_expires_at=now,
        refresh_token_expires_at=now,
        last_used_at=None,
        status="active",
        metadata_json={},
        created_at=now,
        updated_at=now,
    )
    assert repo.connection is not None
    repo.connection = replace(
        repo.connection,
        encrypted_refresh_token=encrypt_secret("long-lived-token"),
    )

    with pytest.raises(SocialAuthError, match="Threads OAuth token request was rejected"):
        await SocialAuthService(
            repository=repo,
            oauth_clients={"threads": _client()},
        ).refresh_connection(user_id=_USER_ID, provider="threads")

    assert repo.connection is not None
    assert repo.connection.status == "needs_reauth"
