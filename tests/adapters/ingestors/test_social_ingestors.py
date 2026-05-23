from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any, cast

import pytest
from cryptography.fernet import Fernet

from app.adapters.ingestors.runner import SourceIngestionRunner
from app.adapters.ingestors.threads_user_threads import (
    ThreadsUserThreadsIngester,
    ThreadsUserThreadsIngestionConfig,
)
from app.adapters.ingestors.x_timeline import XTimelineIngester, XTimelineIngestionConfig
from app.application.dto.social_auth import SocialOAuthClientProtocol
from app.application.ports.signal_sources import SignalSourceRepositoryPort
from app.application.ports.social_connections import SocialConnectionRecord
from app.application.services.social_token_service import SocialAccessTokenResolver
from app.core.time_utils import UTC
from app.security.secret_crypto import encrypt_secret, reset_secret_key_cache


@pytest.fixture(autouse=True)
def _crypto_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    reset_secret_key_cache()
    yield
    reset_secret_key_cache()


@dataclass
class _FakeResponse:
    status_code: int
    payload: dict[str, Any]
    headers: dict[str, str] | None = None

    def json(self) -> dict[str, Any]:
        return self.payload


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


class _FakeSocialConnectionRepository:
    def __init__(self, connection: SocialConnectionRecord | None) -> None:
        self.connection = connection
        self.updates: list[Any] = []

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
        update: Any,
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
    async def refresh_access_token(
        self,
        *,
        provider: str,
        refresh_token: str,
        scopes: list[str],
        correlation_id: str | None,
    ) -> Any:
        raise AssertionError("refresh_access_token should not be called")


class _FakeSignalSourceRepository:
    def __init__(self, *, max_items_per_run: int | None = None) -> None:
        self.sources: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.subscriptions: list[dict[str, Any]] = []
        self.successes: list[int] = []
        self.errors: list[dict[str, Any]] = []
        self.run_state = {
            "is_active": True,
            "active_subscription": True,
            "backoff_until": None,
            "max_items_per_run": max_items_per_run,
        }

    async def async_upsert_source(self, **kwargs: Any) -> dict[str, Any]:
        self.sources.append(kwargs)
        return {"id": 1, **kwargs}

    async def async_subscribe(self, **kwargs: Any) -> dict[str, Any]:
        self.subscriptions.append(kwargs)
        return {"id": len(self.subscriptions), **kwargs}

    async def async_get_source_run_state(self, source_id: int) -> dict[str, Any]:
        return self.run_state

    async def async_upsert_feed_item(self, **kwargs: Any) -> dict[str, Any]:
        self.items.append(kwargs)
        return {"id": len(self.items), **kwargs}

    async def async_record_source_fetch_success(self, source_id: int) -> None:
        self.successes.append(source_id)

    async def async_record_source_fetch_error(self, **kwargs: Any) -> bool:
        self.errors.append(kwargs)
        return False


def _connection(
    *,
    provider: str,
    status: str = "active",
    scopes: list[str] | None = None,
) -> SocialConnectionRecord:
    now = dt.datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
    return SocialConnectionRecord(
        id=1,
        user_id=1001,
        provider=provider,
        auth_type="oauth2",
        provider_user_id=f"{provider}-user-id",
        provider_username=f"{provider}_reader",
        encrypted_access_token=encrypt_secret(f"{provider}-access-token"),
        encrypted_refresh_token=encrypt_secret(f"{provider}-refresh-token"),
        token_scopes=scopes,
        access_token_expires_at=None,
        refresh_token_expires_at=None,
        last_used_at=None,
        status=status,
        metadata_json=None,
        created_at=now,
        updated_at=now,
    )


def _resolver(
    connection: SocialConnectionRecord | None,
    *,
    provider: str,
) -> SocialAccessTokenResolver:
    return SocialAccessTokenResolver(
        repository=cast("Any", _FakeSocialConnectionRepository(connection)),
        oauth_clients={provider: cast("SocialOAuthClientProtocol", _FakeOAuthClient())},
    )


@pytest.mark.asyncio
async def test_x_timeline_ingester_persists_items_and_respects_max_items_per_run() -> None:
    client = _FakeClient(
        [
            _FakeResponse(
                200,
                {
                    "data": [
                        {
                            "id": "101",
                            "author_id": "x-user-id",
                            "text": "first post",
                            "created_at": "2026-05-23T10:00:00Z",
                            "public_metrics": {
                                "like_count": 3,
                                "retweet_count": 2,
                                "reply_count": 1,
                            },
                        },
                        {"id": "102", "author_id": "x-user-id", "text": "second post"},
                    ],
                    "includes": {
                        "users": [{"id": "x-user-id", "username": "x_reader", "name": "Reader"}]
                    },
                },
            )
        ]
    )
    ingester = XTimelineIngester(
        config=XTimelineIngestionConfig(
            enabled=True,
            user_id=1001,
            timeline_mode="user_posts",
            limit=30,
            api_base_url="https://api.x.test/2",
        ),
        token_resolver=_resolver(
            _connection(provider="x", scopes=["tweet.read", "users.read"]),
            provider="x",
        ),
        client=cast("Any", client),
    )
    repo = _FakeSignalSourceRepository(max_items_per_run=1)
    runner = SourceIngestionRunner(
        repository=cast("SignalSourceRepositoryPort", repo),
        ingesters=[ingester],
        subscriber_user_ids=[1001],
    )

    stats = await runner.run_once()

    assert stats == {"enabled": 1, "sources": 1, "items": 1, "errors": 0, "skipped": 0}
    assert client.calls[0]["url"] == "https://api.x.test/2/users/x-user-id/tweets"
    assert repo.items[0]["external_id"] == "x:101"
    assert repo.items[0]["canonical_url"] == "https://x.com/x_reader/status/101"
    assert repo.items[0]["engagement"]["comments"] == 1
    rendered = repr(repo.sources) + repr(repo.items)
    assert "x-access-token" not in rendered
    assert "x-refresh-token" not in rendered


@pytest.mark.asyncio
async def test_x_home_timeline_uses_reverse_chronological_endpoint() -> None:
    client = _FakeClient([_FakeResponse(200, {"data": []})])
    ingester = XTimelineIngester(
        config=XTimelineIngestionConfig(
            enabled=True,
            user_id=1001,
            timeline_mode="home_timeline",
            api_base_url="https://api.x.test/2",
        ),
        token_resolver=_resolver(
            _connection(provider="x", scopes=["tweet.read", "users.read"]),
            provider="x",
        ),
        client=cast("Any", client),
    )

    await ingester.fetch()

    assert (
        client.calls[0]["url"]
        == "https://api.x.test/2/users/x-user-id/timelines/reverse_chronological"
    )


@pytest.mark.asyncio
async def test_threads_user_threads_ingester_persists_me_threads_items() -> None:
    client = _FakeClient(
        [
            _FakeResponse(
                200,
                {
                    "data": [
                        {
                            "id": "thread-1",
                            "text": "hello threads",
                            "username": "threads_reader",
                            "timestamp": "2026-05-23T10:00:00+0000",
                            "permalink": "https://www.threads.net/@threads_reader/post/abc",
                            "media_type": "TEXT_POST",
                        }
                    ]
                },
            )
        ]
    )
    ingester = ThreadsUserThreadsIngester(
        config=ThreadsUserThreadsIngestionConfig(
            enabled=True,
            user_id=1001,
            limit=30,
            graph_base_url="https://graph.threads.test/v1.0",
        ),
        token_resolver=_resolver(
            _connection(provider="threads", scopes=["threads_basic"]),
            provider="threads",
        ),
        client=cast("Any", client),
    )
    repo = _FakeSignalSourceRepository()
    runner = SourceIngestionRunner(
        repository=cast("SignalSourceRepositoryPort", repo),
        ingesters=[ingester],
        subscriber_user_ids=[1001],
    )

    stats = await runner.run_once()

    assert stats == {"enabled": 1, "sources": 1, "items": 1, "errors": 0, "skipped": 0}
    assert client.calls[0]["url"] == "https://graph.threads.test/v1.0/me/threads"
    assert client.calls[0]["params"]["fields"]
    assert repo.items[0]["external_id"] == "threads:thread-1"
    assert repo.items[0]["author"] == "threads_reader"
    rendered = repr(repo.sources) + repr(repo.items)
    assert "threads-access-token" not in rendered


@pytest.mark.asyncio
async def test_rate_limit_reset_is_recorded_as_source_backoff() -> None:
    retry_epoch = 1_779_523_000
    client = _FakeClient(
        [
            _FakeResponse(
                429, {"error": "rate limit"}, headers={"x-rate-limit-reset": str(retry_epoch)}
            )
        ]
    )
    ingester = XTimelineIngester(
        config=XTimelineIngestionConfig(enabled=True, user_id=1001),
        token_resolver=_resolver(
            _connection(provider="x", scopes=["tweet.read", "users.read"]),
            provider="x",
        ),
        client=cast("Any", client),
    )
    repo = _FakeSignalSourceRepository()
    runner = SourceIngestionRunner(
        repository=cast("SignalSourceRepositoryPort", repo),
        ingesters=[ingester],
        subscriber_user_ids=[1001],
    )

    stats = await runner.run_once()

    assert stats == {"enabled": 1, "sources": 0, "items": 0, "errors": 1, "skipped": 0}
    assert repo.errors[0]["retry_at"] == dt.datetime.fromtimestamp(retry_epoch, tz=UTC)


@pytest.mark.asyncio
async def test_needs_reauth_connection_is_skipped_without_provider_call() -> None:
    client = _FakeClient([])
    ingester = ThreadsUserThreadsIngester(
        config=ThreadsUserThreadsIngestionConfig(enabled=True, user_id=1001),
        token_resolver=_resolver(
            _connection(
                provider="threads",
                status="needs_reauth",
                scopes=["threads_basic"],
            ),
            provider="threads",
        ),
        client=cast("Any", client),
    )
    repo = _FakeSignalSourceRepository()
    runner = SourceIngestionRunner(
        repository=cast("SignalSourceRepositoryPort", repo),
        ingesters=[ingester],
        subscriber_user_ids=[1001],
    )

    stats = await runner.run_once()

    assert stats == {"enabled": 1, "sources": 1, "items": 0, "errors": 0, "skipped": 0}
    assert client.calls == []
    assert repo.items == []
    assert repo.successes == [1]
