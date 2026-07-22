"""Postgres-backed tests for the UI-managed credential store.

Skips cleanly without ``TEST_DATABASE_URL`` (see tests/conftest.py::database).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from app.config.credential_catalog import CATALOG, NEVER_UI_MANAGED
from app.infrastructure.persistence.credential_store import (
    CredentialStore,
    UnknownCredentialError,
)
from tests.db_helpers_async import upsert_user

OWNER = 4242
INTRUDER = 9999
# Deliberately not OPENROUTER_API_KEY: these tests unset the key under test, and
# that one is required by load_config(), which secret_crypto calls for the
# Fernet material -- unsetting it would break encryption rather than the store.
KEY = "ELEVENLABS_API_KEY"


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide Fernet key material and reset the cached MultiFernet."""
    from app.security.secret_crypto import reset_secret_key_cache

    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    # secret_crypto resolves its Fernet material through load_config(), which
    # requires the first-run provider key regardless of what this test exercises.
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    reset_secret_key_cache()
    yield
    reset_secret_key_cache()


async def _seed_users(database) -> None:
    async with database.session() as session:
        for uid, is_owner in ((OWNER, True), (INTRUDER, False)):
            await upsert_user(session, telegram_user_id=uid, is_owner=is_owner)
        await session.execute(
            text("DELETE FROM service_credentials WHERE user_id = ANY(:ids)"),
            {"ids": [OWNER, INTRUDER]},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_set_then_resolve_round_trips(database, monkeypatch) -> None:
    monkeypatch.delenv(KEY, raising=False)
    await _seed_users(database)
    store = CredentialStore(database)

    hint = await store.set_credential(user_id=OWNER, key=KEY, value="sk-or-secret-value")

    assert hint == "...alue"
    assert await store.resolve(KEY, user_id=OWNER) == "sk-or-secret-value"


@pytest.mark.asyncio
async def test_db_value_overrides_environment(database, monkeypatch) -> None:
    monkeypatch.setenv(KEY, "env-key")
    await _seed_users(database)
    store = CredentialStore(database)

    assert await store.resolve(KEY, user_id=OWNER) == "env-key"

    await store.set_credential(user_id=OWNER, key=KEY, value="db-key")
    # Hot reload: the write invalidates the cache, no restart, no TTL wait.
    assert await store.resolve(KEY, user_id=OWNER) == "db-key"

    await store.delete_credential(user_id=OWNER, key=KEY)
    assert await store.resolve(KEY, user_id=OWNER) == "env-key"


@pytest.mark.asyncio
async def test_credentials_are_scoped_per_user(database, monkeypatch) -> None:
    """The user_id predicate is an IDOR guard -- CLAUDE.md operating rule 12."""
    monkeypatch.delenv(KEY, raising=False)
    await _seed_users(database)
    store = CredentialStore(database)

    await store.set_credential(user_id=OWNER, key=KEY, value="owner-only")

    assert await store.resolve(KEY, user_id=INTRUDER) is None
    assert await store.delete_credential(user_id=INTRUDER, key=KEY) is False
    assert await store.resolve(KEY, user_id=OWNER) == "owner-only"


@pytest.mark.asyncio
async def test_plaintext_is_not_stored(database, monkeypatch) -> None:
    monkeypatch.delenv(KEY, raising=False)
    await _seed_users(database)
    store = CredentialStore(database)
    secret = "sk-plaintext-must-not-persist"

    await store.set_credential(user_id=OWNER, key=KEY, value=secret)

    async with database.session() as session:
        raw = await session.scalar(
            text("SELECT encrypted_value FROM service_credentials WHERE user_id = :uid"),
            {"uid": OWNER},
        )
    assert secret.encode() not in bytes(raw)


@pytest.mark.asyncio
async def test_list_status_never_exposes_values(database, monkeypatch) -> None:
    monkeypatch.delenv(KEY, raising=False)
    await _seed_users(database)
    store = CredentialStore(database)
    await store.set_credential(user_id=OWNER, key=KEY, value="sk-super-secret")

    statuses = await store.list_status(user_id=OWNER)

    assert len(statuses) == len(CATALOG)
    entry = next(s for s in statuses if s.key == KEY)
    assert entry.configured_in_db is True
    assert entry.hint == "...cret"
    serialized = repr(statuses)
    assert "sk-super-secret" not in serialized


@pytest.mark.asyncio
async def test_forbidden_keys_are_rejected(database) -> None:
    await _seed_users(database)
    store = CredentialStore(database)

    for forbidden in sorted(NEVER_UI_MANAGED):
        with pytest.raises(UnknownCredentialError):
            await store.set_credential(user_id=OWNER, key=forbidden, value="nope")
