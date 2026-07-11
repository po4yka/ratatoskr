from __future__ import annotations

import datetime as dt
import os
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import delete, select

from app.config.database import DatabaseConfig
from app.core.time_utils import UTC
from app.db.models import ClientSecret, RefreshToken, User
from app.db.session import Database
from app.infrastructure.persistence.repositories.auth_repository import (
    AuthRepositoryAdapter,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class FakeTokenCache:
    def __init__(self) -> None:
        self.tokens: dict[str, dict[str, Any]] = {}
        self.revoked: list[str] = []

    async def set_token(
        self,
        token_hash: str,
        *,
        user_id: int,
        client_id: str | None,
        expires_at: dt.datetime,
        is_revoked: bool,
        token_id: int,
        remember_me: bool,
        family_id: str,
        parent_token_hash: str | None,
    ) -> None:
        self.tokens[token_hash] = {
            "id": token_id,
            "user": user_id,
            "user_id": user_id,
            "client_id": client_id,
            "expires_at": expires_at,
            "is_revoked": is_revoked,
            "remember_me": remember_me,
            "family_id": family_id,
            "parent_token_hash": parent_token_hash,
        }

    async def get_token(self, token_hash: str) -> dict[str, Any] | None:
        return self.tokens.get(token_hash)

    async def mark_revoked(self, token_hash: str) -> None:
        self.revoked.append(token_hash)
        if token_hash in self.tokens:
            self.tokens[token_hash]["is_revoked"] = True


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture
async def database() -> AsyncGenerator[Database]:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres repository tests")

    db = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    await db.migrate()
    await _clear(db)
    try:
        yield db
    finally:
        await _clear(db)
        await db.dispose()


async def _clear(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(delete(RefreshToken))
        await session.execute(delete(ClientSecret))
        await session.execute(delete(User))


async def _create_user(database: Database, user_id: int = 9401) -> int:
    async with database.transaction() as session:
        session.add(User(telegram_user_id=user_id, username=f"user-{user_id}"))
    return user_id


@pytest.mark.asyncio
async def test_auth_repository_refresh_token_sessions_and_cache(database: Database) -> None:
    user_id = await _create_user(database)
    cache = FakeTokenCache()
    repo = AuthRepositoryAdapter(database, token_cache=cast("Any", cache))
    expires_at = dt.datetime.now(UTC) + dt.timedelta(days=7)

    first_id = await repo.async_create_refresh_token(
        user_id=user_id,
        token_hash="token-a",
        client_id="mobile",
        device_info="iPhone",
        ip_address="127.0.0.1",
        expires_at=expires_at,
        family_id="fam-a",
    )
    second_id = await repo.async_create_refresh_token(
        user_id=user_id,
        token_hash="token-b",
        client_id="web",
        device_info="Safari",
        ip_address="127.0.0.2",
        expires_at=expires_at,
        family_id="fam-b",
    )

    assert cache.tokens["token-a"] == {
        "id": first_id,
        "user": user_id,
        "user_id": user_id,
        "client_id": "mobile",
        "expires_at": expires_at,
        "is_revoked": False,
        "remember_me": True,
        "family_id": "fam-a",
        "parent_token_hash": None,
    }

    # Simulate an expired Redis entry so the repository reloads PostgreSQL and
    # repopulates the cache through its read-through path.
    cache.tokens.clear()
    cached = await repo.async_get_refresh_token_by_hash("token-a")
    assert cached is not None
    assert cached["id"] == first_id
    assert cached["user"] == user_id
    assert cached["remember_me"] is True
    assert cached["family_id"] == "fam-a"
    assert cached["parent_token_hash"] is None

    await repo.async_update_refresh_token_last_used(first_id)
    sessions = await repo.async_list_active_sessions(user_id, dt.datetime.now(UTC))
    assert {session["id"] for session in sessions} == {first_id, second_id}

    assert await repo.async_revoke_session_by_id(second_id, user_id) is True
    assert "token-b" in cache.revoked
    sessions = await repo.async_list_active_sessions(user_id, dt.datetime.now(UTC))
    assert [session["id"] for session in sessions] == [first_id]

    assert await repo.async_revoke_refresh_token("token-a") is True
    assert await repo.async_revoke_refresh_token("missing") is False
    assert set(cache.revoked) >= {"token-a", "token-b"}


@pytest.mark.asyncio
async def test_auth_repository_revokes_all_user_tokens(database: Database) -> None:
    user_id = await _create_user(database)
    other_user_id = await _create_user(database, user_id=9402)
    cache = FakeTokenCache()
    repo = AuthRepositoryAdapter(database, token_cache=cast("Any", cache))
    expires_at = dt.datetime.now(UTC) + dt.timedelta(days=7)

    for token_hash, owner in [
        ("token-1", user_id),
        ("token-2", user_id),
        ("token-3", other_user_id),
    ]:
        await repo.async_create_refresh_token(
            user_id=owner,
            token_hash=token_hash,
            client_id=None,
            device_info=None,
            ip_address=None,
            expires_at=expires_at,
            family_id=f"fam-{token_hash}",
        )

    assert await repo.async_revoke_all_user_tokens(user_id) == 2
    assert set(cache.revoked) == {"token-1", "token-2"}
    remaining = await repo.async_list_active_sessions(other_user_id, dt.datetime.now(UTC))
    assert [session["token_hash"] for session in remaining] == ["token-3"]


@pytest.mark.asyncio
async def test_auth_repository_client_secret_lifecycle(database: Database) -> None:
    user_id = await _create_user(database)
    repo = AuthRepositoryAdapter(database)

    first_id = await repo.async_create_client_secret(
        user_id=user_id,
        client_id="ios",
        secret_hash="hash-1",
        secret_salt="salt-1",
        label="Phone",
    )
    latest = await repo.async_get_client_secret(user_id, "ios")
    assert latest is not None
    assert latest["id"] == first_id
    assert latest["user"] == user_id

    replacement_id = await repo.async_replace_active_client_secret(
        user_id=user_id,
        client_id="ios",
        secret_hash="hash-2",
        secret_salt="salt-2",
        label="Phone 2",
    )
    assert replacement_id != first_id
    active = await repo.async_get_client_secret(user_id, "ios")
    assert active is not None
    assert active["id"] == replacement_id

    secrets = await repo.async_list_client_secrets(user_id=user_id, client_id="ios")
    assert [secret["status"] for secret in secrets] == ["revoked", "active"]

    locked = await repo.async_increment_failed_attempts(
        replacement_id,
        max_attempts=1,
        lockout_minutes=5,
    )
    assert locked["status"] == "locked"
    assert locked["failed_attempts"] == 1
    assert locked["locked_until"] is not None

    await repo.async_reset_failed_attempts(replacement_id)
    reset = await repo.async_get_client_secret_by_id(replacement_id)
    assert reset is not None
    assert reset["failed_attempts"] == 0
    assert reset["locked_until"] is None

    await repo.async_update_client_secret(replacement_id, label="Renamed", unknown="ignored")
    updated = await repo.async_get_client_secret_by_id(replacement_id)
    assert updated is not None
    assert updated["label"] == "Renamed"

    async with database.session() as session:
        stored = await session.scalar(select(ClientSecret).where(ClientSecret.id == first_id))
    assert stored is not None
    assert stored.status == "revoked"


@pytest.mark.asyncio
async def test_auth_repository_rejects_missing_user_for_client_secret(database: Database) -> None:
    repo = AuthRepositoryAdapter(database)

    with pytest.raises(ValueError, match="User 999 not found"):
        await repo.async_create_client_secret(
            user_id=999,
            client_id="ios",
            secret_hash="hash",
            secret_salt="salt",
        )
