"""Tests for the encrypted AI backup session store."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy.sql.dml import Delete, Update

from app.adapters.ai_backup.session_store import (
    AiBackupSessionStore,
    validate_storage_state,
    validate_storage_state_shape,
)
from app.db.models.ai_backup import AiBackupService
from app.security.secret_crypto import InvalidEncryptedSecretError, encrypt_secret

if TYPE_CHECKING:
    from app.db.session import Database


class _FakeSession:
    def __init__(self, state: dict) -> None:
        self._state = state

    async def scalar(self, _stmt: object) -> object:
        return self._state.get("row")

    def add(self, obj: object) -> None:
        self._state["row"] = obj

    async def execute(self, stmt: object) -> object:
        if isinstance(stmt, Delete):
            self._state.pop("row", None)
            return SimpleNamespace(rowcount=1)
        if isinstance(stmt, Update):
            params = stmt.compile().params
            current = self._state.get("row")
            expected_revision = params["encrypted_cookies_1"]
            if current is None or current.encrypted_cookies != expected_revision:
                return SimpleNamespace(rowcount=0)
            current.encrypted_cookies = params["encrypted_cookies"]
            return SimpleNamespace(rowcount=1)
        raise AssertionError(f"Unexpected statement: {stmt!r}")


class _FakeCtx:
    def __init__(self, session: _FakeSession) -> None:
        self._s = session

    async def __aenter__(self) -> _FakeSession:
        return self._s

    async def __aexit__(self, *_a: object) -> bool:
        return False


class FakeDb:
    def __init__(self) -> None:
        self.state: dict = {}

    def session(self) -> _FakeCtx:
        return _FakeCtx(_FakeSession(self.state))

    def transaction(self) -> _FakeCtx:
        return _FakeCtx(_FakeSession(self.state))


def _store(db: FakeDb | None = None) -> AiBackupSessionStore:
    return AiBackupSessionStore(cast("Database", db or FakeDb()))


@pytest.fixture
def _fernet(monkeypatch) -> None:
    from cryptography.fernet import Fernet, MultiFernet

    mf = MultiFernet([Fernet(Fernet.generate_key())])
    monkeypatch.setattr("app.security.secret_crypto._get_multi_fernet", lambda: mf)


@pytest.mark.parametrize(
    "bad",
    ["string", 123, None, {"no_cookies": []}, {"cookies": "notalist"}],
)
def test_validate_rejects_bad_shapes(bad: object) -> None:
    with pytest.raises(ValueError, match="storage_state"):
        validate_storage_state_shape(bad)


def test_validate_shape_accepts_minimal() -> None:
    validate_storage_state_shape({"cookies": []})


@pytest.mark.parametrize(
    ("service", "name", "domain"),
    [
        (AiBackupService.CHATGPT, "__Secure-next-auth.session-token", ".chatgpt.com"),
        (AiBackupService.CHATGPT, "__Secure-next-auth.session-token.0", "chatgpt.com"),
        (AiBackupService.CLAUDE, "sessionKey", ".claude.ai"),
    ],
)
def test_validate_accepts_usable_service_cookie(
    service: AiBackupService, name: str, domain: str
) -> None:
    validate_storage_state(
        service,
        {"cookies": [{"name": name, "domain": domain, "value": "secret", "expires": -1}]},
        now_timestamp=100,
    )


@pytest.mark.parametrize(
    "cookie",
    [
        {"name": "cf_clearance", "domain": ".chatgpt.com", "value": "cf", "expires": -1},
        {
            "name": "__Secure-next-auth.session-token",
            "domain": ".chatgpt.com.evil.example",
            "value": "secret",
            "expires": -1,
        },
        {
            "name": "__Secure-next-auth.session-token",
            "domain": ".chatgpt.com",
            "value": "secret",
            "expires": 99,
        },
        {
            "name": "__Secure-next-auth.session-token",
            "domain": ".chatgpt.com",
            "value": "",
            "expires": -1,
        },
    ],
)
def test_validate_rejects_missing_wrong_or_expired_session_cookie(cookie: dict) -> None:
    with pytest.raises(ValueError, match="no usable chatgpt session cookie"):
        validate_storage_state(
            AiBackupService.CHATGPT,
            {"cookies": [cookie]},
            now_timestamp=100,
        )


async def test_load_absent_returns_none() -> None:
    store = _store()
    assert await store.load(1, AiBackupService.CHATGPT) is None


@pytest.mark.usefixtures("_fernet")
async def test_roundtrip_encrypts() -> None:
    db = FakeDb()
    store = _store(db)
    state = {
        "cookies": [
            {
                "name": "sessionKey",
                "domain": ".claude.ai",
                "value": "Привет",
                "expires": -1,
            }
        ],
        "origins": [],
    }
    await store.save(7, AiBackupService.CLAUDE, state)
    # Stored blob is real ciphertext, not the plaintext JSON.
    assert db.state["row"].encrypted_cookies != json.dumps(state, ensure_ascii=False).encode()
    loaded = await store.load(7, AiBackupService.CLAUDE)
    assert loaded == state


@pytest.mark.usefixtures("_fernet")
@pytest.mark.parametrize("plaintext", ["not-json", "[]", '{"cookies": []}'])
async def test_load_wraps_malformed_decrypted_state(plaintext: str) -> None:
    db = FakeDb()
    db.state["row"] = SimpleNamespace(encrypted_cookies=encrypt_secret(plaintext))
    store = _store(db)

    with pytest.raises(InvalidEncryptedSecretError, match="browser session is invalid"):
        await store.load(7, AiBackupService.CLAUDE)


@pytest.mark.usefixtures("_fernet")
async def test_save_rejects_bad_shape_before_db() -> None:
    db = FakeDb()
    store = _store(db)
    with pytest.raises(ValueError, match="storage_state"):
        await store.save(1, AiBackupService.CHATGPT, {"no_cookies": True})
    assert "row" not in db.state  # nothing written


async def test_delete_removes_session_and_is_idempotent() -> None:
    db = FakeDb()
    db.state["row"] = SimpleNamespace(encrypted_cookies=b"ciphertext")
    store = _store(db)

    await store.delete(7, AiBackupService.CLAUDE)
    await store.delete(7, AiBackupService.CLAUDE)

    assert "row" not in db.state


@pytest.mark.usefixtures("_fernet")
async def test_refresh_does_not_recreate_revoked_session() -> None:
    db = FakeDb()
    store = _store(db)
    state = {
        "cookies": [
            {
                "name": "sessionKey",
                "domain": ".claude.ai",
                "value": "refreshed",
                "expires": -1,
            }
        ]
    }

    refreshed = await store.refresh(
        7,
        AiBackupService.CLAUDE,
        state,
        expected_revision=b"revoked-session-revision",
    )

    assert refreshed is False
    assert "row" not in db.state


@pytest.mark.usefixtures("_fernet")
async def test_refresh_does_not_overwrite_session_replaced_after_load() -> None:
    db = FakeDb()
    store = _store(db)
    original = {
        "cookies": [{"name": "sessionKey", "domain": ".claude.ai", "value": "A", "expires": -1}]
    }
    replacement = {
        "cookies": [{"name": "sessionKey", "domain": ".claude.ai", "value": "B", "expires": -1}]
    }
    stale_refresh = {
        "cookies": [
            {
                "name": "sessionKey",
                "domain": ".claude.ai",
                "value": "A-refreshed",
                "expires": -1,
            }
        ]
    }

    await store.save(7, AiBackupService.CLAUDE, original)
    loaded = await store.load_for_refresh(7, AiBackupService.CLAUDE)
    assert loaded is not None

    # Owner revokes A and ingests B while A's backup is still in flight.
    await store.delete(7, AiBackupService.CLAUDE)
    await store.save(7, AiBackupService.CLAUDE, replacement)

    refreshed = await store.refresh(
        7,
        AiBackupService.CLAUDE,
        stale_refresh,
        expected_revision=loaded.revision,
    )

    assert refreshed is False
    assert await store.load(7, AiBackupService.CLAUDE) == replacement
