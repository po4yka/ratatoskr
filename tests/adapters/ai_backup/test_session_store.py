"""Tests for the encrypted AI backup session store."""

from __future__ import annotations

import json

import pytest

from app.adapters.ai_backup.session_store import (
    AiBackupSessionStore,
    validate_storage_state,
    validate_storage_state_shape,
)
from app.db.models.ai_backup import AiBackupService


class _FakeSession:
    def __init__(self, state: dict) -> None:
        self._state = state

    async def scalar(self, _stmt: object) -> object:
        return self._state.get("row")

    def add(self, obj: object) -> None:
        self._state["row"] = obj


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
    store = AiBackupSessionStore(FakeDb())
    assert await store.load(1, AiBackupService.CHATGPT) is None


@pytest.mark.usefixtures("_fernet")
async def test_roundtrip_encrypts() -> None:
    db = FakeDb()
    store = AiBackupSessionStore(db)
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
async def test_save_rejects_bad_shape_before_db() -> None:
    db = FakeDb()
    store = AiBackupSessionStore(db)
    with pytest.raises(ValueError, match="storage_state"):
        await store.save(1, AiBackupService.CHATGPT, {"no_cookies": True})
    assert "row" not in db.state  # nothing written
