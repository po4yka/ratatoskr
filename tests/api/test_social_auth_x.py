from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.adapters.social.x import XOAuthClient, XOAuthConfig
from app.api.routers.auth.tokens import create_access_token
from app.application.ports.social_connections import SocialConnectionUpsert
from app.application.services.social_auth_service import SocialAuthError, SocialAuthService
from app.config import clear_config_cache
from app.db.models import SocialConnection
from app.infrastructure.persistence.repositories.social_connection_repository import (
    SocialConnectionRepositoryAdapter,
)
from app.security.secret_crypto import decrypt_secret, encrypt_secret, reset_secret_key_cache

_USER_ID = 778_001
_FERNET_KEY = Fernet.generate_key().decode("ascii")
_REDIRECT_URI = "https://app.example.com/social/x/callback"


@pytest_asyncio.fixture(autouse=True)
async def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_USER_IDS", str(_USER_ID))
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "")
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", _FERNET_KEY)
    monkeypatch.setenv("X_OAUTH_CLIENT_ID", "x-client-id")
    monkeypatch.setenv("X_OAUTH_REDIRECT_URI", _REDIRECT_URI)
    monkeypatch.setenv("X_OAUTH_SCOPES", "tweet.read users.read offline.access")
    reset_secret_key_cache()
    clear_config_cache()
    yield
    clear_config_cache()
    reset_secret_key_cache()


@pytest_asyncio.fixture
async def x_user(db: Any, user_factory: Any) -> Any:
    return await user_factory(telegram_user_id=_USER_ID, username="x-oauth-user")


def _auth_headers() -> dict[str, str]:
    token = create_access_token(_USER_ID, client_id="test")
    return {"Authorization": f"Bearer {token}", "X-Correlation-ID": "cid-x-oauth-api-test"}


def test_x_connect_url_uses_configured_client_and_default_read_scopes(
    client: Any,
    x_user: Any,
) -> None:
    response = client.get(
        "/v1/social/x/connect-url",
        headers=_auth_headers(),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    parsed = urlparse(data["connectUrl"])
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://x.com/i/oauth2/authorize"
    assert query["client_id"] == ["x-client-id"]
    assert query["state"] == [data["state"]]
    assert query["scope"] == ["tweet.read users.read offline.access"]
    assert query["code_challenge_method"] == ["S256"]
    assert data["scopes"] == ["tweet.read", "users.read", "offline.access"]


async def test_x_callback_exchanges_code_and_stores_encrypted_tokens(
    client: Any,
    db: Any,
    x_user: Any,
    respx_mock: Any,
) -> None:
    token_route = respx_mock.post("https://api.x.com/2/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "token_type": "bearer",
                "access_token": "api-access-token-secret",
                "refresh_token": "api-refresh-token-secret",
                "expires_in": 7200,
                "scope": "tweet.read users.read offline.access",
            },
        )
    )
    respx_mock.get("https://api.x.com/2/users/me").mock(
        return_value=httpx.Response(200, json={"data": {"id": "12345", "username": "ratatoskr_x"}})
    )
    connect = client.get(
        "/v1/social/x/connect-url",
        params={"redirectUri": _REDIRECT_URI},
        headers=_auth_headers(),
    )
    assert connect.status_code == 200, connect.text

    response = client.post(
        "/v1/social/x/callback",
        json={
            "code": "provider-code-secret",
            "state": connect.json()["data"]["state"],
            "redirectUri": _REDIRECT_URI,
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 200, response.text
    assert "api-access-token-secret" not in response.text
    assert "api-refresh-token-secret" not in response.text
    connection = response.json()["data"]["connection"]
    assert connection["provider"] == "x"
    assert connection["providerUserId"] == "12345"
    assert connection["providerUsername"] == "ratatoskr_x"
    assert connection["tokenScopes"] == ["tweet.read", "users.read", "offline.access"]
    assert connection["status"] == "active"

    token_form = parse_qs(token_route.calls[0].request.content.decode())
    assert token_form["grant_type"] == ["authorization_code"]
    assert token_form["code"] == ["provider-code-secret"]
    assert token_form["client_id"] == ["x-client-id"]

    async with db.session() as session:
        row = await session.scalar(
            select(SocialConnection).where(
                SocialConnection.user_id == _USER_ID,
                SocialConnection.provider == "x",
            )
        )
    assert row is not None
    assert row.encrypted_access_token is not None
    assert row.encrypted_refresh_token is not None
    assert decrypt_secret(row.encrypted_access_token) == "api-access-token-secret"
    assert decrypt_secret(row.encrypted_refresh_token) == "api-refresh-token-secret"


async def test_x_refresh_rotates_stored_access_token(
    db: Any,
    x_user: Any,
    respx_mock: Any,
) -> None:
    repository = SocialConnectionRepositoryAdapter(db)
    await repository.upsert_connection(
        SocialConnectionUpsert(
            user_id=_USER_ID,
            provider="x",
            auth_type="oauth2",
            encrypted_access_token=encrypt_secret("old-access-token"),
            encrypted_refresh_token=encrypt_secret("old-refresh-token"),
            token_scopes=["tweet.read", "users.read", "offline.access"],
            status="active",
        )
    )
    respx_mock.post("https://api.x.com/2/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "rotated-access-token",
                "refresh_token": "rotated-refresh-token",
                "expires_in": 7200,
                "scope": "tweet.read users.read offline.access",
            },
        )
    )
    service = SocialAuthService(
        repository=repository,
        oauth_clients={"x": XOAuthClient(XOAuthConfig(client_id="x-client-id"))},
    )

    result = await service.refresh_connection(
        user_id=_USER_ID,
        provider="x",
        correlation_id="cid-refresh",
    )

    assert result.connection.status == "active"
    async with db.session() as session:
        row = await session.scalar(
            select(SocialConnection).where(
                SocialConnection.user_id == _USER_ID,
                SocialConnection.provider == "x",
            )
        )
    assert row is not None
    assert row.encrypted_access_token is not None
    assert row.encrypted_refresh_token is not None
    assert decrypt_secret(row.encrypted_access_token) == "rotated-access-token"
    assert decrypt_secret(row.encrypted_refresh_token) == "rotated-refresh-token"


async def test_x_refresh_failure_marks_connection_needs_reauth(
    db: Any,
    x_user: Any,
    respx_mock: Any,
) -> None:
    repository = SocialConnectionRepositoryAdapter(db)
    await repository.upsert_connection(
        SocialConnectionUpsert(
            user_id=_USER_ID,
            provider="x",
            auth_type="oauth2",
            encrypted_access_token=encrypt_secret("old-access-token"),
            encrypted_refresh_token=encrypt_secret("old-refresh-token"),
            token_scopes=["tweet.read", "users.read", "offline.access"],
            status="active",
        )
    )
    respx_mock.post("https://api.x.com/2/oauth2/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    service = SocialAuthService(
        repository=repository,
        oauth_clients={"x": XOAuthClient(XOAuthConfig(client_id="x-client-id"))},
    )

    with pytest.raises(SocialAuthError, match="X OAuth token request was rejected"):
        await service.refresh_connection(
            user_id=_USER_ID,
            provider="x",
            correlation_id="cid-refresh",
        )

    async with db.session() as session:
        row = await session.scalar(
            select(SocialConnection).where(
                SocialConnection.user_id == _USER_ID,
                SocialConnection.provider == "x",
            )
        )
    assert row is not None
    assert row.status == "needs_reauth"
