"""Tests for refresh-token delivery policy: web cookie vs mobile/CLI body."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.parametrize(
    ("client_id", "expected"),
    [
        ("webapp", True),
        ("web-frontend", True),
        ("mobile-ios", False),
        ("mobile-android", False),
        ("cli-1", False),
        ("mcp-server", False),
        ("automation-script", False),
        ("foobar", False),
        (None, False),
    ],
)
def test_is_web_client(client_id, expected):
    from app.api.routers.auth.tokens import is_web_client

    assert is_web_client(client_id) is expected


# ---------------------------------------------------------------------------
# /credentials-login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_id", "expect_cookie"),
    [
        ("webapp", True),
        ("mobile-ios", False),
    ],
)
async def test_credentials_login_token_delivery(client_id, expect_cookie):
    from app.api.models.auth import CredentialsLoginRequest
    from app.api.routers.auth.endpoints_credentials import credentials_login

    response = MagicMock()
    payload = CredentialsLoginRequest(
        identifier="testuser",
        password="TestPass123!",
        client_id=client_id,
        remember_me=True,
    )

    mock_cred_repo = AsyncMock()
    mock_cred_repo.async_get_by_canonical = AsyncMock(
        return_value={
            "id": 1,
            "user_id": 123,
            "password_hash": "phc",
            "pepper_version": 1,
            "failed_attempts": 0,
            "locked_until": None,
        }
    )
    mock_cred_repo.async_reset_failure = AsyncMock()
    mock_cred_repo.async_touch_last_login = AsyncMock()

    mock_user_repo = AsyncMock()
    mock_user_repo.async_get_user_by_telegram_id = AsyncMock(
        return_value={"telegram_user_id": 123, "username": "testuser"}
    )

    mock_cfg = MagicMock()
    mock_cfg.auth.credentials_remember_me_days = 30
    mock_cfg.auth.credentials_no_remember_hours = 12

    with (
        patch("app.api.routers.auth.endpoints_credentials.validate_client_id"),
        patch("app.api.routers.auth.endpoints_credentials.validate_password"),
        patch("app.api.routers.auth.endpoints_credentials.ensure_user_allowed"),
        patch(
            "app.api.routers.auth.endpoints_credentials.verify_password",
            return_value=(True, False),
        ),
        patch(
            "app.api.routers.auth.endpoints_credentials.get_user_credential_repository",
            return_value=mock_cred_repo,
        ),
        patch(
            "app.api.routers.auth.endpoints_credentials.get_user_repository",
            return_value=mock_user_repo,
        ),
        patch(
            "app.api.routers.auth.endpoints_credentials.load_config",
            return_value=mock_cfg,
        ),
        patch(
            "app.api.routers.auth.endpoints_credentials.create_access_token",
            return_value="acc.tok",
        ),
        patch(
            "app.api.routers.auth.endpoints_credentials.create_refresh_token",
            new_callable=AsyncMock,
            return_value=("ref.tok", 42),
        ),
    ):
        result = await credentials_login(payload, response)

    tokens = result["data"]["tokens"]
    if expect_cookie:
        response.set_cookie.assert_called_once()
        assert tokens["refreshToken"] is None
    else:
        response.set_cookie.assert_not_called()
        assert tokens["refreshToken"] == "ref.tok"


# ---------------------------------------------------------------------------
# /telegram-login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_id", "expect_cookie"),
    [
        ("webapp", True),
        ("mobile-ios", False),
    ],
)
async def test_telegram_login_token_delivery(client_id, expect_cookie):
    from app.api.models.auth import TelegramLoginRequest
    from app.api.routers.auth.endpoints_telegram import telegram_login

    response = MagicMock()
    payload = TelegramLoginRequest(
        telegram_user_id=123456789,
        auth_hash="abc123",
        auth_date=1700000000,
        client_id=client_id,
        username="testuser",
    )

    mock_user_repo = AsyncMock()
    mock_user_repo.async_get_or_create_user = AsyncMock(
        return_value=({"telegram_user_id": 123456789, "username": "testuser"}, False)
    )

    with (
        patch("app.api.routers.auth.endpoints_telegram.validate_client_id"),
        patch("app.api.routers.auth.endpoints_telegram.verify_telegram_auth"),
        patch(
            "app.api.routers.auth.endpoints_telegram.get_user_repository",
            return_value=mock_user_repo,
        ),
        patch(
            "app.api.routers.auth.endpoints_telegram.create_access_token",
            return_value="acc.tok",
        ),
        patch(
            "app.api.routers.auth.endpoints_telegram.create_refresh_token",
            new_callable=AsyncMock,
            return_value=("ref.tok", 42),
        ),
    ):
        result = await telegram_login(payload, response)

    tokens = result["data"]["tokens"]
    if expect_cookie:
        response.set_cookie.assert_called_once()
        assert tokens["refreshToken"] is None
    else:
        response.set_cookie.assert_not_called()
        assert tokens["refreshToken"] == "ref.tok"


# ---------------------------------------------------------------------------
# /secret-login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_id", "expect_cookie"),
    [
        ("webapp", True),
        ("cli-1", False),
    ],
)
async def test_secret_login_token_delivery(client_id, expect_cookie):
    from app.api.models.auth import SecretLoginRequest
    from app.api.routers.auth.endpoints_secret_keys import secret_login

    response = MagicMock()
    # SecretLoginRequest uses `secret` (not `secret_key`) and requires `user_id`
    payload = SecretLoginRequest(
        user_id=123456789,
        client_id=client_id,
        secret="sk-test-value-here",
    )

    mock_auth_repo = AsyncMock()
    mock_auth_repo.async_get_client_secret = AsyncMock(
        return_value={
            "id": 1,
            "user_id": 123456789,
            "secret_hash": "hashed",
            "secret_salt": "salt",
            "status": "active",
            "locked_until": None,
            "failed_attempts": 0,
        }
    )
    mock_auth_repo.async_update_client_secret = AsyncMock()

    mock_user_repo = AsyncMock()
    mock_user_repo.async_get_user_by_telegram_id = AsyncMock(
        return_value={"telegram_user_id": 123456789, "username": "testuser"}
    )

    with (
        patch("app.api.routers.auth.endpoints_secret_keys.ensure_secret_login_enabled"),
        patch("app.api.routers.auth.endpoints_secret_keys.validate_client_id"),
        patch("app.api.routers.auth.endpoints_secret_keys.ensure_user_allowed"),
        patch(
            "app.api.routers.auth.endpoints_secret_keys.get_auth_repository",
            return_value=mock_auth_repo,
        ),
        patch(
            "app.api.routers.auth.endpoints_secret_keys.get_user_repository",
            return_value=mock_user_repo,
        ),
        patch(
            "app.api.routers.auth.endpoints_secret_keys.validate_secret_value",
            return_value="sk-test-value-here",
        ),
        patch(
            "app.api.routers.auth.endpoints_secret_keys.verify_secret",
            return_value=True,
        ),
        patch(
            "app.api.routers.auth.endpoints_secret_keys.reset_failed_attempts",
            new_callable=AsyncMock,
        ),
        patch(
            "app.api.routers.auth.endpoints_secret_keys.create_access_token",
            return_value="acc.tok",
        ),
        patch(
            "app.api.routers.auth.endpoints_secret_keys.create_refresh_token",
            new_callable=AsyncMock,
            return_value=("ref.tok", 42),
        ),
    ):
        result = await secret_login(payload, response)

    tokens = result["data"]["tokens"]
    if expect_cookie:
        response.set_cookie.assert_called_once()
        assert tokens["refreshToken"] is None
    else:
        response.set_cookie.assert_not_called()
        assert tokens["refreshToken"] == "ref.tok"


# ---------------------------------------------------------------------------
# /refresh — integration tests against real DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_id", "expect_cookie"),
    [
        ("webapp", True),
        ("mobile-ios", False),
    ],
)
async def test_refresh_token_delivery(db, user_factory, client_id, expect_cookie):
    from app.api.models.auth import RefreshTokenRequest
    from app.api.routers.auth.dependencies import get_auth_repository
    from app.api.routers.auth.endpoints_sessions import refresh_access_token
    from app.api.routers.auth.tokens import create_refresh_token

    user = await user_factory(telegram_user_id=987654321, username="delivery_test")
    token, _ = await create_refresh_token(
        user_id=user.telegram_user_id,
        client_id=client_id,
    )

    request = MagicMock()
    request.cookies = {}
    response = MagicMock()
    body = RefreshTokenRequest(refresh_token=token)
    auth_repo = get_auth_repository()

    result = await refresh_access_token(
        request=request,
        response=response,
        refresh_data=body,
        auth_repo=auth_repo,
    )

    tokens = result["data"]["tokens"]
    if expect_cookie:
        response.set_cookie.assert_called_once()
        assert tokens["refreshToken"] is None
    else:
        response.set_cookie.assert_not_called()
        refresh = tokens["refreshToken"]
        assert isinstance(refresh, str) and refresh
        assert refresh != tokens["accessToken"]
        assert refresh != token
