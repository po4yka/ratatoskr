from __future__ import annotations

import datetime as dt
import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, select

from app.application.ports.social_connections import (
    SocialConnectionRepositoryPort,
    SocialConnectionUpdate,
    SocialConnectionUpsert,
)
from app.config.database import DatabaseConfig
from app.core.time_utils import UTC
from app.db.models import SocialConnection, User
from app.db.session import Database
from app.infrastructure.persistence.repositories.social_connection_repository import (
    SocialConnectionRepositoryAdapter,
)
from app.security.secret_crypto import decrypt_secret, encrypt_secret

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


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
        await session.execute(delete(SocialConnection))
        await session.execute(delete(User))


async def _create_user(database: Database, user_id: int = 9701) -> int:
    async with database.transaction() as session:
        session.add(User(telegram_user_id=user_id, username=f"social-{user_id}"))
    return user_id


@pytest.mark.asyncio
async def test_social_connection_repository_upserts_and_loads_encrypted_tokens(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    user_id = await _create_user(database)
    repo = SocialConnectionRepositoryAdapter(database)
    assert isinstance(repo, SocialConnectionRepositoryPort)

    access_ciphertext = encrypt_secret("x-access-token")
    refresh_ciphertext = encrypt_secret("x-refresh-token")
    expires_at = dt.datetime.now(UTC) + dt.timedelta(hours=1)

    created = await repo.upsert_connection(
        SocialConnectionUpsert(
            user_id=user_id,
            provider="x",
            auth_type="oauth2",
            provider_user_id="x-123",
            provider_username="example_user",
            encrypted_access_token=access_ciphertext,
            encrypted_refresh_token=refresh_ciphertext,
            token_scopes=["tweet.read", "users.read"],
            access_token_expires_at=expires_at,
            status="active",
            metadata_json={"source": "test"},
        )
    )

    assert created.user_id == user_id
    assert created.provider == "x"
    assert created.encrypted_access_token == access_ciphertext
    assert decrypt_secret(created.encrypted_access_token or b"") == "x-access-token"
    assert created.without_tokens().encrypted_access_token is None
    assert created.without_tokens().encrypted_refresh_token is None

    loaded = await repo.get_by_user_and_provider(user_id, "x")
    assert loaded is not None
    assert loaded.id == created.id
    assert loaded.token_scopes == ["tweet.read", "users.read"]

    replacement_ciphertext = encrypt_secret("x-access-token-2")
    updated = await repo.upsert_connection(
        SocialConnectionUpsert(
            user_id=user_id,
            provider="x",
            auth_type="oauth2",
            provider_user_id="x-123",
            provider_username="renamed_user",
            encrypted_access_token=replacement_ciphertext,
            encrypted_refresh_token=refresh_ciphertext,
            token_scopes=["tweet.read"],
            status="needs_reauth",
        )
    )

    assert updated.id == created.id
    assert updated.provider_username == "renamed_user"
    assert updated.status == "needs_reauth"
    assert decrypt_secret(updated.encrypted_access_token or b"") == "x-access-token-2"


@pytest.mark.asyncio
async def test_social_connection_repository_patches_existing_connection(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    user_id = await _create_user(database, user_id=9702)
    repo = SocialConnectionRepositoryAdapter(database)

    await repo.upsert_connection(
        SocialConnectionUpsert(
            user_id=user_id,
            provider="instagram",
            auth_type="oauth2",
            encrypted_access_token=encrypt_secret("ig-access"),
        )
    )

    updated = await repo.update_connection(
        user_id,
        "instagram",
        SocialConnectionUpdate(
            provider_username="ig_user",
            status="revoked",
            metadata_json={"revoked_reason": "user_request"},
        ),
    )

    assert updated is not None
    assert updated.provider == "instagram"
    assert updated.provider_username == "ig_user"
    assert updated.status == "revoked"
    assert updated.metadata_json == {"revoked_reason": "user_request"}

    async with database.session() as session:
        rows = list((await session.execute(select(SocialConnection))).scalars())
    assert len(rows) == 1
    assert rows[0].encrypted_access_token is not None


@pytest.mark.asyncio
async def test_social_connection_repository_rejects_unsupported_provider(
    database: Database,
) -> None:
    user_id = await _create_user(database, user_id=9703)
    repo = SocialConnectionRepositoryAdapter(database)

    with pytest.raises(ValueError, match="Unsupported social provider"):
        await repo.get_by_user_and_provider(user_id, "github")
