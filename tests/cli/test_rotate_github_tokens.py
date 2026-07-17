"""Tests for app.cli.rotate_github_tokens."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from app.security.token_crypto import reset_key_cache


@pytest.fixture(autouse=True)
def _reset_crypto_cache(monkeypatch: pytest.MonkeyPatch):
    # Pre-reset is the real protection against cross-test cache pollution.
    # Earlier tests in the suite may have set these env vars directly via
    # os.environ (bypassing monkeypatch); strip them here so each test
    # starts with a clean Fernet key environment.
    monkeypatch.delenv("GITHUB_TOKEN_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN_PREVIOUS_KEYS", raising=False)
    reset_key_cache()
    yield
    reset_key_cache()


def _make_row(user_id: int, encrypted_token: bytes) -> MagicMock:
    row = MagicMock()
    row.id = user_id
    row.user_id = user_id
    row.encrypted_token = encrypted_token
    return row


def _make_browser_row(row_id: int, user_id: int, encrypted_cookies: bytes) -> MagicMock:
    row = MagicMock()
    row.id = row_id
    row.user_id = user_id
    row.encrypted_cookies = encrypted_cookies
    return row


def _scalar_result(rows: list[MagicMock]) -> MagicMock:
    return MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows))))


def _import_reencrypt():
    from app.cli.rotate_github_tokens import reencrypt_all_tokens

    return reencrypt_all_tokens


@pytest.mark.asyncio
async def test_reencrypt_row_encrypted_with_old_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row encrypted with an old key is re-encrypted with the primary key."""
    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    old_ct = Fernet(old_key).encrypt(b"ghp_secret")
    row = _make_row(1, old_ct)

    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", new_key.decode("ascii"))
    monkeypatch.setenv("GITHUB_TOKEN_PREVIOUS_KEYS", old_key.decode("ascii"))

    db = MagicMock()
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_scalar_result([row]), _scalar_result([])])
    session.get = AsyncMock(return_value=row)
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)
    db.transaction.return_value.__aenter__ = AsyncMock(return_value=session)
    db.transaction.return_value.__aexit__ = AsyncMock(return_value=False)

    reencrypt_all_tokens = _import_reencrypt()
    result = await reencrypt_all_tokens(db, dry_run=False)

    db.transaction.assert_called_once()
    assert result.processed == 1
    assert result.reencrypted == 1
    assert result.failed == 0
    # Verify the stored ciphertext is now readable by the primary key alone
    new_ct = row.encrypted_token
    assert Fernet(new_key).decrypt(new_ct) == b"ghp_secret"


@pytest.mark.asyncio
async def test_dry_run_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True reports would-be changes but does not open a transaction."""
    key = Fernet.generate_key()
    ct = Fernet(key).encrypt(b"ghp_dryrun")
    row = _make_row(2, ct)
    original_ct = ct

    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", key.decode("ascii"))
    monkeypatch.delenv("GITHUB_TOKEN_PREVIOUS_KEYS", raising=False)

    db = MagicMock()
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_scalar_result([row]), _scalar_result([])])
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)

    reencrypt_all_tokens = _import_reencrypt()
    result = await reencrypt_all_tokens(db, dry_run=True)

    assert result.processed == 1
    assert result.reencrypted == 1
    assert result.failed == 0
    # No transaction opened in dry-run mode
    db.transaction.assert_not_called()
    # Row object is unchanged
    assert row.encrypted_token == original_ct


@pytest.mark.asyncio
async def test_undecryptable_row_counted_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row with garbage ciphertext is logged and counted as failed — loop continues."""
    new_key = Fernet.generate_key()
    row = _make_row(3, b"this is not valid fernet ciphertext")

    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", new_key.decode("ascii"))
    monkeypatch.delenv("GITHUB_TOKEN_PREVIOUS_KEYS", raising=False)

    db = MagicMock()
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_scalar_result([row]), _scalar_result([])])
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)

    reencrypt_all_tokens = _import_reencrypt()
    result = await reencrypt_all_tokens(db, dry_run=False)

    assert result.processed == 1
    assert result.reencrypted == 0
    assert result.failed == 1


@pytest.mark.asyncio
async def test_user_id_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """user_id parameter restricts which rows are fetched."""
    key = Fernet.generate_key()
    ct = Fernet(key).encrypt(b"ghp_filtered")
    row = _make_row(42, ct)

    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", key.decode("ascii"))
    monkeypatch.delenv("GITHUB_TOKEN_PREVIOUS_KEYS", raising=False)

    db = MagicMock()
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_scalar_result([row]), _scalar_result([])])
    session.get = AsyncMock(return_value=row)
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)
    db.transaction.return_value.__aenter__ = AsyncMock(return_value=session)
    db.transaction.return_value.__aexit__ = AsyncMock(return_value=False)

    reencrypt_all_tokens = _import_reencrypt()
    result = await reencrypt_all_tokens(db, dry_run=False, user_id=42)

    db.transaction.assert_called_once()
    assert result.processed == 1
    assert result.reencrypted == 1


@pytest.mark.asyncio
async def test_reencrypts_all_browser_sessions_with_old_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    first = _make_browser_row(10, 42, Fernet(old_key).encrypt(b'{"cookies": []}'))
    second = _make_browser_row(11, 42, Fernet(old_key).encrypt(b'{"cookies": [{"name":"x"}]}'))

    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", new_key.decode("ascii"))
    monkeypatch.setenv("GITHUB_TOKEN_PREVIOUS_KEYS", old_key.decode("ascii"))

    db = MagicMock()
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[_scalar_result([]), _scalar_result([first, second])]
    )
    session.get = AsyncMock(side_effect=[first, second])
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)
    db.transaction.return_value.__aenter__ = AsyncMock(return_value=session)
    db.transaction.return_value.__aexit__ = AsyncMock(return_value=False)

    reencrypt_all_tokens = _import_reencrypt()
    result = await reencrypt_all_tokens(db)

    assert result.processed == 2
    assert result.reencrypted == 2
    assert result.failed == 0
    assert result.github_tokens_processed == 0
    assert result.browser_sessions_processed == 2
    assert Fernet(new_key).decrypt(first.encrypted_cookies) == b'{"cookies": []}'
    assert Fernet(new_key).decrypt(second.encrypted_cookies) == b'{"cookies": [{"name":"x"}]}'


@pytest.mark.asyncio
async def test_concurrent_browser_session_update_is_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Fernet.generate_key()
    stale = _make_browser_row(12, 42, Fernet(key).encrypt(b"old state"))
    fresh = _make_browser_row(12, 42, Fernet(key).encrypt(b"new state"))

    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", key.decode("ascii"))

    db = MagicMock()
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_scalar_result([]), _scalar_result([stale])])
    session.get = AsyncMock(return_value=fresh)
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)
    db.transaction.return_value.__aenter__ = AsyncMock(return_value=session)
    db.transaction.return_value.__aexit__ = AsyncMock(return_value=False)

    reencrypt_all_tokens = _import_reencrypt()
    result = await reencrypt_all_tokens(db)

    assert result.failed == 1
    assert Fernet(key).decrypt(fresh.encrypted_cookies) == b"new state"
