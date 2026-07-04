"""Credentials (nickname/email + password) login endpoint tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.api.exceptions import AuthenticationError
from app.api.models.auth import ChangePasswordRequest, CredentialsLoginRequest
from app.api.routers.auth import credential_auth, endpoints as auth_endpoints
from app.core.time_utils import UTC
from app.db.models import RefreshToken, User, UserCredential

if TYPE_CHECKING:
    from app.db.session import Database


OWNER_ID = 123456789
NICKNAME = "owner"
EMAIL = "owner@example.com"
PASSWORD = "correct horse battery staple!"


def _mock_response() -> MagicMock:
    return MagicMock()


def _configure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIALS_LOGIN_PEPPER", "x" * 32)
    monkeypatch.setenv("CREDENTIALS_LOGIN_MAX_FAILED_ATTEMPTS", "3")
    monkeypatch.setenv("CREDENTIALS_LOGIN_LOCKOUT_MINUTES", "1")
    monkeypatch.setenv("CREDENTIALS_LOGIN_PASSWORD_MIN_LENGTH", "8")
    monkeypatch.setenv("CREDENTIALS_LOGIN_REMEMBER_ME_DAYS", "30")
    monkeypatch.setenv("CREDENTIALS_LOGIN_NO_REMEMBER_HOURS", "12")
    # Speed up argon2 for the test suite -- production cfg uses defaults.
    monkeypatch.setenv("CREDENTIALS_LOGIN_ARGON2_TIME_COST", "1")
    monkeypatch.setenv("CREDENTIALS_LOGIN_ARGON2_MEMORY_KIB", "8192")
    monkeypatch.setenv("ALLOWED_USER_IDS", str(OWNER_ID))
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "")
    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "test_api_hash_placeholder_value___")
    monkeypatch.setenv("BOT_TOKEN", "1000000000:TESTTOKENPLACEHOLDER1234567890ABC")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "dummy-firecrawl-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-openrouter-key")
    # Reset module-level caches so the new env wins.
    credential_auth._cfg_holder[0] = None
    credential_auth._hasher_holder[0] = None
    credential_auth._decoy_phc_holder[0] = None


async def _create_owner_with_credential(
    db: Database, *, telegram_user_id: int = OWNER_ID, password: str = PASSWORD
) -> tuple[User, UserCredential]:
    phc, version = credential_auth.hash_password(password)
    nickname_display, nickname_canonical = credential_auth.canonicalize_nickname(NICKNAME)
    email_display, email_canonical = credential_auth.canonicalize_email(EMAIL)
    async with db.transaction() as session:
        user = User(telegram_user_id=telegram_user_id, username=nickname_display, is_owner=True)
        session.add(user)
        await session.flush()
        cred = UserCredential(
            user_id=telegram_user_id,
            nickname=nickname_display,
            nickname_canonical=nickname_canonical,
            email=email_display,
            email_canonical=email_canonical,
            password_hash=phc,
            pepper_version=version,
        )
        session.add(cred)
        await session.flush()
        return user, cred


def _login_request(**overrides) -> CredentialsLoginRequest:
    payload = {
        "identifier": NICKNAME,
        "password": PASSWORD,
        "remember_me": False,
        "client_id": "web-v1",
    }
    payload.update(overrides)
    return CredentialsLoginRequest(**payload)  # type: ignore[arg-type]


# ----------------- Identifier canonicalization -----------------


def test_canonicalize_identifier_routes_by_at_sign():
    kind, display, canonical = credential_auth.canonicalize_identifier("OwNeR")
    assert kind == "nickname"
    assert display == "OwNeR"
    assert canonical == "owner"

    kind, display, canonical = credential_auth.canonicalize_identifier("Owner@Example.COM")
    assert kind == "email"
    assert display == "Owner@Example.COM"
    assert canonical == "owner@example.com"


def test_canonicalize_identifier_strips_and_normalizes_whitespace():
    kind, display, canonical = credential_auth.canonicalize_identifier("  Café  ")
    assert kind == "nickname"
    assert display == "Café"
    assert canonical == "café"


# ----------------- Happy paths -----------------


async def test_credentials_login_success_with_nickname(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_owner_with_credential(db)

    response = await auth_endpoints.credentials_login(
        _login_request(),
        _mock_response(),
    )

    assert response["data"]["tokens"]["accessToken"]
    assert response["data"]["sessionId"] is not None

    async with db.session() as session:
        token = await session.scalar(select(RefreshToken).limit(1))
    assert token is not None
    assert token.remember_me is False
    # Short-TTL family: ~12 hours from now.
    assert (token.expires_at - datetime.now(UTC)).total_seconds() < 13 * 3600


async def test_credentials_login_success_with_email_and_remember_me(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_owner_with_credential(db)

    response = await auth_endpoints.credentials_login(
        _login_request(identifier="OwNeR@Example.COM", remember_me=True),
        _mock_response(),
    )

    assert response["data"]["tokens"]["accessToken"]
    async with db.session() as session:
        token = await session.scalar(select(RefreshToken).limit(1))
    assert token is not None
    assert token.remember_me is True
    # Long-TTL family: > 29 days from now.
    assert (token.expires_at - datetime.now(UTC)).total_seconds() > 29 * 24 * 3600


async def test_credentials_login_succeeds_after_case_fold(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_owner_with_credential(db)

    response = await auth_endpoints.credentials_login(
        _login_request(identifier="OWNER"),
        _mock_response(),
    )
    assert response["data"]["tokens"]["accessToken"]


# ----------------- Failure paths (anti-enumeration) -----------------


async def test_credentials_login_unknown_identifier_returns_generic_401(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_owner_with_credential(db)

    with pytest.raises(AuthenticationError, match="Invalid credentials"):
        await auth_endpoints.credentials_login(
            _login_request(identifier="ghost"),
            _mock_response(),
        )


async def test_credentials_login_wrong_password_returns_generic_401(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_owner_with_credential(db)

    with pytest.raises(AuthenticationError, match="Invalid credentials"):
        await auth_endpoints.credentials_login(
            _login_request(password="wrong-password-1234"),
            _mock_response(),
        )


async def test_credentials_login_user_not_in_allowlist_returns_generic_401(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    monkeypatch.setenv("ALLOWED_USER_IDS", "999999")  # owner not in list
    credential_auth._cfg_holder[0] = None
    await _create_owner_with_credential(db)

    with pytest.raises(AuthenticationError, match="Invalid credentials"):
        await auth_endpoints.credentials_login(_login_request(), _mock_response())


# ----------------- Lockout -----------------


async def test_credentials_login_lockout_after_repeated_failures(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    _, cred = await _create_owner_with_credential(db)

    bad = _login_request(password="wrong-password-1234")
    for _ in range(3):
        with pytest.raises(AuthenticationError):
            await auth_endpoints.credentials_login(bad, _mock_response())

    async with db.session() as session:
        reloaded = await session.get(UserCredential, cred.id)
    assert reloaded is not None
    assert reloaded.failed_attempts >= 3
    assert reloaded.locked_until is not None
    # 6th attempt: even with the correct password, the lockout window blocks login.
    err: AuthenticationError | None = None
    try:
        await auth_endpoints.credentials_login(_login_request(), _mock_response())
    except AuthenticationError as raised:
        err = raised
    assert err is not None
    assert err.retry_after is not None
    assert err.retry_after > 0


async def test_credentials_login_resets_failures_on_success(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    _, cred = await _create_owner_with_credential(db)

    with pytest.raises(AuthenticationError):
        await auth_endpoints.credentials_login(
            _login_request(password="wrong-password-1234"), _mock_response()
        )
    async with db.session() as session:
        reloaded = await session.get(UserCredential, cred.id)
    assert reloaded is not None
    assert reloaded.failed_attempts == 1

    await auth_endpoints.credentials_login(_login_request(), _mock_response())

    async with db.session() as session:
        reloaded2 = await session.get(UserCredential, cred.id)
    assert reloaded2 is not None
    assert reloaded2.failed_attempts == 0
    assert reloaded2.locked_until is None
    assert reloaded2.last_login_at is not None


# ----------------- Pepper-not-configured gate -----------------


async def test_credentials_login_without_pepper_raises_configuration_error(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pepper presence is the only gate -- no separate enabled flag.

    Bootstrap the credential row first (hashing needs a pepper), then strip
    the env var and reset the module-level cache so the next request sees an
    unconfigured deployment and surfaces ``ConfigurationError``.
    """
    from app.api.exceptions import ConfigurationError

    _configure_env(monkeypatch)
    await _create_owner_with_credential(db)

    # Now simulate a deploy that lost (or never set) the pepper.
    from app.config.settings import clear_config_cache

    monkeypatch.delenv("CREDENTIALS_LOGIN_PEPPER", raising=False)
    clear_config_cache()
    credential_auth._cfg_holder[0] = None
    credential_auth._hasher_holder[0] = None
    credential_auth._decoy_phc_holder[0] = None

    with pytest.raises(ConfigurationError):
        await auth_endpoints.credentials_login(_login_request(), _mock_response())


# ----------------- Change password -----------------


async def test_change_password_requires_current_password(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.dependencies.database import get_auth_repository

    _configure_env(monkeypatch)
    _, cred = await _create_owner_with_credential(db)

    new_password = "an entirely new passphrase 9999"
    user_ctx = {"user_id": OWNER_ID, "client_id": "web-v1", "username": NICKNAME}
    auth_repo = get_auth_repository()

    with pytest.raises(AuthenticationError):
        await auth_endpoints.change_password(
            ChangePasswordRequest(current_password="wrong", new_password=new_password),
            user=user_ctx,
            auth_repo=auth_repo,
        )

    response = await auth_endpoints.change_password(
        ChangePasswordRequest(current_password=PASSWORD, new_password=new_password),
        user=user_ctx,
        auth_repo=auth_repo,
    )
    assert response["data"]["message"]

    async with db.session() as session:
        reloaded = await session.get(UserCredential, cred.id)
    assert reloaded is not None
    matched, _ = credential_auth.verify_password(
        new_password, reloaded.password_hash, reloaded.pepper_version
    )
    assert matched


async def test_change_password_revokes_existing_refresh_token_families(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refresh token issued before a password change must not survive it.

    Regression test for the audit finding: change_password previously only
    verified the old password and stored the new hash, leaving any stolen
    refresh token valid for up to its full 30-day TTL. It must now revoke
    every active refresh-token family for the user, mirroring logout-all.
    """
    from app.api.dependencies.database import get_auth_repository
    from app.api.routers.auth.tokens import create_refresh_token

    _configure_env(monkeypatch)
    await _create_owner_with_credential(db)

    # Simulate a pre-existing session (e.g. a leaked refresh token) that
    # should stop working once the password changes.
    _token, session_id = await create_refresh_token(OWNER_ID, "web-v1", remember_me=True)
    async with db.session() as session:
        pre_change = await session.get(RefreshToken, session_id)
    assert pre_change is not None
    assert pre_change.is_revoked is False

    user_ctx = {"user_id": OWNER_ID, "client_id": "web-v1", "username": NICKNAME}
    auth_repo = get_auth_repository()
    new_password = "an entirely new passphrase 9999"

    response = await auth_endpoints.change_password(
        ChangePasswordRequest(current_password=PASSWORD, new_password=new_password),
        user=user_ctx,
        auth_repo=auth_repo,
    )
    assert response["data"]["message"]

    async with db.session() as session:
        post_change = await session.get(RefreshToken, session_id)
    assert post_change is not None
    assert post_change.is_revoked is True


# ----------------- Repository (atomic counter) -----------------


async def test_record_failure_is_atomic(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent record_failure calls must produce final counter == 2."""
    _configure_env(monkeypatch)
    _, cred = await _create_owner_with_credential(db)

    from app.api.dependencies.database import get_user_credential_repository

    repo = get_user_credential_repository(session_manager=db)

    import asyncio

    await asyncio.gather(
        repo.async_record_failure(cred.id, max_attempts=10, lockout_minutes=1),
        repo.async_record_failure(cred.id, max_attempts=10, lockout_minutes=1),
    )

    async with db.session() as session:
        reloaded = await session.get(UserCredential, cred.id)
    assert reloaded is not None
    assert reloaded.failed_attempts == 2


# ----------------- Existing telegram tokens default remember_me=True -----------------


async def test_legacy_refresh_tokens_default_remember_me_true(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telegram/secret-login callers don't pass remember_me; column must default to True."""
    _configure_env(monkeypatch)
    async with db.transaction() as session:
        user = User(telegram_user_id=OWNER_ID, username="owner", is_owner=True)
        session.add(user)
        await session.flush()

    from app.api.routers.auth.tokens import create_refresh_token

    _token, session_id = await create_refresh_token(
        OWNER_ID,
        client_id="web-v1",
    )
    async with db.session() as session:
        record = await session.get(RefreshToken, session_id)
    assert record is not None
    assert record.remember_me is True
    assert (record.expires_at - datetime.now(UTC)) > timedelta(days=29)
