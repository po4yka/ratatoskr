"""Secret login + secret-key management endpoint tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.api.exceptions import AuthenticationError, AuthorizationError, ResourceNotFoundError
from app.api.models.auth import (
    SecretKeyCreateRequest,
    SecretKeyRevokeRequest,
    SecretKeyRotateRequest,
    SecretLoginRequest,
)
from app.api.routers.auth import endpoints as auth_endpoints, secret_auth
from app.db.models import ClientSecret, User

if TYPE_CHECKING:
    from app.db.session import Database


def _mock_response() -> MagicMock:
    return MagicMock()


def _configure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_LOGIN_ENABLED", "1")
    monkeypatch.setenv("SECRET_LOGIN_MIN_LENGTH", "12")
    monkeypatch.setenv("SECRET_LOGIN_MAX_LENGTH", "128")
    monkeypatch.setenv("SECRET_LOGIN_MAX_FAILED_ATTEMPTS", "2")
    monkeypatch.setenv("SECRET_LOGIN_LOCKOUT_MINUTES", "1")
    # SECRET_LOGIN_PEPPER is required when secret-login is enabled — there is
    # no JWT-key fallback. 64-char hex test value matches the >=32 floor.
    monkeypatch.setenv(
        "SECRET_LOGIN_PEPPER",
        "test-pepper-32chars-minimum-for-secret-login-fixtures-do-not-reuse",
    )
    monkeypatch.setenv("ALLOWED_USER_IDS", "123456789")
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "")
    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "test_api_hash_placeholder_value___")
    monkeypatch.setenv("BOT_TOKEN", "1000000000:TESTTOKENPLACEHOLDER1234567890ABC")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "dummy-firecrawl-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-openrouter-key")
    secret_auth._cfg_holder[0] = None


async def _create_owner(db: Database, telegram_user_id: int = 123456789) -> User:
    async with db.transaction() as session:
        user = User(telegram_user_id=telegram_user_id, username="owner", is_owner=True)
        session.add(user)
        await session.flush()
        return user


async def test_secret_login_success(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    user = await _create_owner(db)

    secret_value, record = await secret_auth.build_secret_record(
        user.telegram_user_id,
        "mobile-client",
        provided_secret="secret-value-strong",
        label="primary",
        description=None,
        expires_at=None,
    )

    response = await auth_endpoints.secret_login(
        SecretLoginRequest(
            user_id=123456789,
            client_id="mobile-client",
            secret=secret_value,
            username="owner",
        ),
        _mock_response(),
    )

    assert response["data"]["tokens"]["accessToken"]
    assert "sessionId" in response["data"]
    assert response["data"]["sessionId"] is not None

    async with db.session() as session:
        reloaded = await session.get(ClientSecret, record["id"])
    assert reloaded is not None
    assert reloaded.last_used_at is not None
    assert reloaded.failed_attempts == 0
    assert reloaded.status == "active"


async def test_secret_login_lockout(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    user = await _create_owner(db)

    await secret_auth.build_secret_record(
        user.telegram_user_id,
        "mobile-client",
        provided_secret="secret-value-strong",
        label="primary",
        description=None,
        expires_at=None,
    )

    bad_request = SecretLoginRequest(
        user_id=123456789,
        client_id="mobile-client",
        secret="wrong-secret-12",
        username="owner",
    )

    with pytest.raises(AuthenticationError):
        await auth_endpoints.secret_login(bad_request, _mock_response())

    with pytest.raises((AuthenticationError, AuthorizationError)):
        await auth_endpoints.secret_login(bad_request, _mock_response())

    async with db.session() as session:
        record = await session.scalar(select(ClientSecret).limit(1))
    assert record is not None
    assert record.status == "locked"
    assert record.failed_attempts >= 2
    assert record.locked_until is not None


def test_run_decoy_secret_verify_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The decoy runs a real argon2 verify (constant cost) and never raises."""
    _configure_env(monkeypatch)
    secret_auth._decoy_phc_holder[0] = None

    assert secret_auth._get_decoy_phc().startswith("$argon2")
    # Always mismatches, result discarded, must return None without raising.
    assert secret_auth.run_decoy_secret_verify("any-provided-secret") is None


def _spy_decoy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the decoy verify with a recorder and return the capture list.

    Patches the name where secret_login uses it (imported into
    endpoints_secret_keys), so calls on the not-found paths are observed.
    """
    called: list[str] = []
    monkeypatch.setattr(
        "app.api.routers.auth.endpoints_secret_keys.run_decoy_secret_verify",
        lambda secret: called.append(secret),
    )
    return called


def _patch_repos(
    monkeypatch: pytest.MonkeyPatch, *, user: object | None, secret_record: object | None
) -> None:
    user_repo = MagicMock()
    user_repo.async_get_user_by_telegram_id = AsyncMock(return_value=user)
    auth_repo = MagicMock()
    auth_repo.async_get_client_secret = AsyncMock(return_value=secret_record)
    monkeypatch.setattr(
        "app.api.routers.auth.endpoints_secret_keys.get_user_repository", lambda: user_repo
    )
    monkeypatch.setattr(
        "app.api.routers.auth.endpoints_secret_keys.get_auth_repository", lambda: auth_repo
    )


async def test_secret_login_runs_decoy_verify_when_secret_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User exists but has no registered secret: the not-found path must still pay
    the argon2 cost so it is timing-indistinguishable from a wrong-secret verify."""
    _configure_env(monkeypatch)
    _patch_repos(monkeypatch, user={"telegram_user_id": 123456789}, secret_record=None)
    called = _spy_decoy(monkeypatch)

    with pytest.raises(AuthenticationError):
        await auth_endpoints.secret_login(
            SecretLoginRequest(
                user_id=123456789,
                client_id="mobile-client",
                secret="no-such-secret-strong",
                username="owner",
            ),
            _mock_response(),
        )

    assert called == ["no-such-secret-strong"]


async def test_secret_login_runs_decoy_verify_when_user_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No such user: still pay the argon2 cost before raising."""
    _configure_env(monkeypatch)
    _patch_repos(monkeypatch, user=None, secret_record=None)
    called = _spy_decoy(monkeypatch)

    with pytest.raises(ResourceNotFoundError):
        await auth_endpoints.secret_login(
            SecretLoginRequest(
                user_id=123456789,
                client_id="mobile-client",
                secret="no-such-secret-strong",
                username="owner",
            ),
            _mock_response(),
        )

    assert called == ["no-such-secret-strong"]


async def test_secret_key_management(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    owner = await _create_owner(db)

    create_payload = SecretKeyCreateRequest(
        user_id=owner.telegram_user_id,
        client_id="mobile-client",
        label="primary",
        description="first key",
        expires_at=None,
        secret="management-secret-strong",
        username="owner",
    )

    owner_context = {
        "user_id": owner.telegram_user_id,
        "client_id": "admin",
        "username": "owner",
    }

    create_resp = await auth_endpoints.create_secret_key(create_payload, user=owner_context)
    key = create_resp["data"]["key"]
    assert key["client_id"] == "mobile-client"
    assert key["status"] == "active"

    rotate_resp = await auth_endpoints.rotate_secret_key(
        key["id"],
        SecretKeyRotateRequest(secret="rotated-secret-value"),
        user=owner_context,
    )
    assert rotate_resp["data"]["secret"] == "rotated-secret-value"

    revoke_resp = await auth_endpoints.revoke_secret_key(
        key["id"], SecretKeyRevokeRequest(reason="cleanup"), user=owner_context
    )
    assert revoke_resp["data"]["key"]["status"] == "revoked"

    list_resp = await auth_endpoints.list_secret_keys(user=owner_context)
    assert len(list_resp["data"]["keys"]) == 1


@pytest.mark.parametrize("client_id", ["cli-client", "mcp-client", "automation-client"])
async def test_self_service_secret_key_management_round_trip_for_supported_client_types(
    db: Database, monkeypatch: pytest.MonkeyPatch, client_id: str
) -> None:
    _configure_env(monkeypatch)

    async with db.transaction() as session:
        user = User(telegram_user_id=222222222, username="regular-user", is_owner=False)
        session.add(user)
        await session.flush()

    user_context = {
        "user_id": 222222222,
        "client_id": client_id,
        "username": "regular",
    }

    create_payload = SecretKeyCreateRequest(
        user_id=222222222,
        client_id=client_id,
        label="self-service",
        description="secondary key",
        expires_at=None,
        secret="self-service-secret-strong",
        username="regular-user",
    )

    create_resp = await auth_endpoints.create_secret_key(create_payload, user=user_context)
    created_key = create_resp["data"]["key"]
    assert created_key["user_id"] == 222222222
    assert created_key["client_id"] == client_id
    assert created_key["status"] == "active"

    list_resp = await auth_endpoints.list_secret_keys(user=user_context)
    assert [key["id"] for key in list_resp["data"]["keys"]] == [created_key["id"]]

    rotate_resp = await auth_endpoints.rotate_secret_key(
        created_key["id"],
        SecretKeyRotateRequest(secret="rotated-secret-value"),
        user=user_context,
    )
    assert rotate_resp["data"]["secret"] == "rotated-secret-value"

    revoke_resp = await auth_endpoints.revoke_secret_key(
        created_key["id"],
        SecretKeyRevokeRequest(reason="cleanup"),
        user=user_context,
    )
    assert revoke_resp["data"]["key"]["status"] == "revoked"

    with pytest.raises(AuthenticationError, match="Only active secrets can be rotated"):
        await auth_endpoints.rotate_secret_key(
            created_key["id"],
            SecretKeyRotateRequest(secret="another-rotated-secret"),
            user=user_context,
        )

    second_revoke_resp = await auth_endpoints.revoke_secret_key(
        created_key["id"],
        SecretKeyRevokeRequest(reason="cleanup-again"),
        user=user_context,
    )
    assert second_revoke_resp["data"]["key"]["status"] == "revoked"

    async with db.session() as session:
        reloaded = await session.get(ClientSecret, created_key["id"])
    assert reloaded is not None
    assert reloaded.status == "revoked"


@pytest.mark.parametrize(
    ("payload_user_id", "client_id"),
    [
        (222222222, "mobile-client"),
        (333333333, "cli-client"),
    ],
)
async def test_self_service_secret_key_management_rejects_invalid_scope(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    payload_user_id: int,
    client_id: str,
) -> None:
    _configure_env(monkeypatch)

    async with db.transaction() as session:
        session.add(User(telegram_user_id=222222222, username="regular-user", is_owner=False))

    user_context = {
        "user_id": 222222222,
        "client_id": "cli-client",
        "username": "regular",
    }

    create_payload = SecretKeyCreateRequest(
        user_id=payload_user_id,
        client_id=client_id,
        label="self-service",
        description=None,
        expires_at=None,
        secret="self-service-secret-strong",
        username="regular-user",
    )

    with pytest.raises(AuthorizationError):
        await auth_endpoints.create_secret_key(create_payload, user=user_context)


async def test_secret_key_creation_does_not_promote_target_to_owner(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    monkeypatch.setenv("ALLOWED_USER_IDS", "123456789,222222222")
    secret_auth._cfg_holder[0] = None

    owner = await _create_owner(db)
    owner_context = {
        "user_id": owner.telegram_user_id,
        "client_id": "admin",
        "username": "owner",
    }

    create_payload = SecretKeyCreateRequest(
        user_id=222222222,
        client_id="mobile-client",
        label="target-user-key",
        description=None,
        expires_at=None,
        secret="target-user-secret-strong",
        username="target-user",
    )

    create_resp = await auth_endpoints.create_secret_key(create_payload, user=owner_context)
    assert create_resp["data"]["key"]["client_id"] == "mobile-client"

    async with db.session() as session:
        target_user = await session.scalar(select(User).where(User.telegram_user_id == 222222222))
    assert target_user is not None
    assert target_user.is_owner is False


# ----- pepper / JWT-key decoupling regression tests ----------------------------


def test_get_secret_pepper_returns_configured_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: SECRET_LOGIN_PEPPER returns through, no JWT fallback consulted."""
    _configure_env(monkeypatch)
    pepper = secret_auth._get_secret_pepper()
    assert pepper == ("test-pepper-32chars-minimum-for-secret-login-fixtures-do-not-reuse")


def test_get_secret_pepper_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: with secret-login enabled but no pepper set, _get_secret_pepper
    must raise rather than fall back to the JWT signing key. Rotating
    JWT_SECRET_KEY would otherwise invalidate every stored ClientSecret hash."""
    _configure_env(monkeypatch)
    monkeypatch.delenv("SECRET_LOGIN_PEPPER", raising=False)
    secret_auth._cfg_holder[0] = None

    with pytest.raises(RuntimeError, match="SECRET_LOGIN_PEPPER is unset"):
        secret_auth._get_secret_pepper()


def test_pepper_validator_rejects_short_value() -> None:
    """A pepper shorter than 32 chars must fail config load, not just warn."""
    from app.config.api import AuthConfig

    with pytest.raises(ValueError, match="at least 32 chars"):
        AuthConfig(secret_pepper="short-value")
