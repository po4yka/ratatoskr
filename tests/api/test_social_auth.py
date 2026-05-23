from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.api.routers.auth.tokens import create_access_token
from app.application.dto.social_auth import OAuthTokenResult
from app.core.time_utils import UTC
from app.db.models import SocialAuthState, SocialConnection
from app.security.secret_crypto import decrypt_secret, reset_secret_key_cache

_USER_ID = 777_001
_OTHER_USER_ID = 777_002
_FERNET_KEY = Fernet.generate_key().decode("ascii")
_REDIRECT_URI = "https://app.example.com/social/callback"


@dataclass
class FakeSocialOAuthClient:
    exchanges: list[dict[str, Any]] = field(default_factory=list)

    def build_authorization_url(
        self,
        *,
        provider: str,
        state: str,
        code_challenge: str,
        redirect_uri: str,
        scopes: list[str],
    ) -> str:
        return (
            f"https://oauth.example.com/{provider}/authorize"
            f"?state={state}&code_challenge={code_challenge}"
            f"&redirect_uri={redirect_uri}&scope={' '.join(scopes)}"
        )

    async def exchange_code(
        self,
        *,
        provider: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        scopes: list[str],
        correlation_id: str | None,
    ) -> OAuthTokenResult:
        self.exchanges.append(
            {
                "provider": provider,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "scopes": scopes,
                "correlation_id": correlation_id,
            }
        )
        return OAuthTokenResult(
            access_token=f"{provider}-access-token-secret",
            refresh_token=f"{provider}-refresh-token-secret",
            scopes=scopes,
            access_token_expires_at="2026-05-24T00:00:00Z",
            refresh_token_expires_at="2026-06-23T00:00:00Z",
            provider_user_id=f"{provider}-user-1",
            provider_username=f"{provider}_tester",
            metadata_json={"account_type": "test"},
        )


@pytest_asyncio.fixture(autouse=True)
async def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_USER_IDS", f"{_USER_ID},{_OTHER_USER_ID}")
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "")
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", _FERNET_KEY)
    reset_secret_key_cache()
    yield
    reset_secret_key_cache()


@pytest_asyncio.fixture
async def social_users(db: Any, user_factory: Any) -> tuple[Any, Any]:
    primary = await user_factory(telegram_user_id=_USER_ID, username="social-primary")
    other = await user_factory(telegram_user_id=_OTHER_USER_ID, username="social-other")
    return primary, other


@pytest.fixture
def fake_oauth_clients(client: Any) -> dict[str, FakeSocialOAuthClient]:
    from app.api.routers import social_auth

    clients = {provider: FakeSocialOAuthClient() for provider in ("x", "instagram", "threads")}
    client.app.dependency_overrides[social_auth.get_social_oauth_clients] = lambda: clients
    try:
        yield clients
    finally:
        client.app.dependency_overrides.pop(social_auth.get_social_oauth_clients, None)


def _auth_headers(user_id: int = _USER_ID) -> dict[str, str]:
    token = create_access_token(user_id, client_id="test")
    return {"Authorization": f"Bearer {token}", "X-Correlation-ID": "cid-social-test"}


def _create_connect_url(client: Any, provider: str = "x") -> dict[str, Any]:
    response = client.get(
        f"/v1/social/{provider}/connect-url",
        params=[
            ("redirectUri", _REDIRECT_URI),
            ("scopes", "profile.read"),
            ("scopes", "offline.access"),
        ],
        headers=_auth_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_connect_url_creates_encrypted_oauth_state(
    client: Any,
    db: Any,
    social_users: Any,
    fake_oauth_clients: dict[str, FakeSocialOAuthClient],
) -> None:
    data = _create_connect_url(client, provider="x")

    assert data["provider"] == "x"
    assert data["connectUrl"].startswith("https://oauth.example.com/x/authorize")
    assert data["state"]
    assert data["scopes"] == ["profile.read", "offline.access"]
    assert data["redirectUri"] == _REDIRECT_URI

    async with db.session() as session:
        states = list((await session.execute(select(SocialAuthState))).scalars())

    assert len(states) == 1
    state = states[0]
    assert state.user_id == _USER_ID
    assert state.provider == "x"
    assert state.state_hash != data["state"]
    assert state.encrypted_code_verifier is not None
    assert decrypt_secret(state.encrypted_code_verifier) not in response_text(data)
    assert state.scopes == ["profile.read", "offline.access"]
    assert state.redirect_uri == _REDIRECT_URI


async def test_callback_success_stores_connection_without_returning_raw_tokens(
    client: Any,
    db: Any,
    social_users: Any,
    fake_oauth_clients: dict[str, FakeSocialOAuthClient],
) -> None:
    connect = _create_connect_url(client, provider="instagram")

    response = client.post(
        "/v1/social/instagram/callback",
        json={"code": "provider-code", "state": connect["state"], "redirectUri": _REDIRECT_URI},
        headers=_auth_headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    connection = body["data"]["connection"]
    assert connection["provider"] == "instagram"
    assert connection["connected"] is True
    assert connection["providerUserId"] == "instagram-user-1"
    assert connection["providerUsername"] == "instagram_tester"
    assert "access-token-secret" not in response.text
    assert "refresh-token-secret" not in response.text
    assert "encrypted_access_token" not in response.text

    async with db.session() as session:
        row = await session.scalar(
            select(SocialConnection).where(
                SocialConnection.user_id == _USER_ID,
                SocialConnection.provider == "instagram",
            )
        )
        auth_state = await session.scalar(select(SocialAuthState))

    assert row is not None
    assert row.encrypted_access_token is not None
    assert row.encrypted_refresh_token is not None
    assert decrypt_secret(row.encrypted_access_token) == "instagram-access-token-secret"
    assert decrypt_secret(row.encrypted_refresh_token) == "instagram-refresh-token-secret"
    assert row.token_scopes == ["profile.read", "offline.access"]
    assert auth_state is not None
    assert auth_state.status == "consumed"
    assert fake_oauth_clients["instagram"].exchanges[0]["correlation_id"] == "cid-social-test"


async def test_callback_rejects_expired_state(
    client: Any,
    db: Any,
    social_users: Any,
    fake_oauth_clients: dict[str, FakeSocialOAuthClient],
) -> None:
    connect = _create_connect_url(client, provider="threads")
    async with db.transaction() as session:
        state = await session.scalar(select(SocialAuthState))
        assert state is not None
        state.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    response = client.post(
        "/v1/social/threads/callback",
        json={"code": "provider-code", "state": connect["state"], "redirectUri": _REDIRECT_URI},
        headers=_auth_headers(),
    )

    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["error"]["details"]["reason_code"] == "SOCIAL_AUTH_STATE_EXPIRED"
    assert fake_oauth_clients["threads"].exchanges == []


async def test_callback_rejects_reused_state(
    client: Any,
    social_users: Any,
    fake_oauth_clients: dict[str, FakeSocialOAuthClient],
) -> None:
    connect = _create_connect_url(client, provider="x")
    payload = {"code": "provider-code", "state": connect["state"], "redirectUri": _REDIRECT_URI}

    first = client.post("/v1/social/x/callback", json=payload, headers=_auth_headers())
    second = client.post("/v1/social/x/callback", json=payload, headers=_auth_headers())

    assert first.status_code == 200, first.text
    assert second.status_code == 409
    assert second.json()["error"]["details"]["reason_code"] == "SOCIAL_AUTH_STATE_REUSED"
    assert len(fake_oauth_clients["x"].exchanges) == 1


async def test_callback_rejects_wrong_user(
    client: Any,
    social_users: Any,
    fake_oauth_clients: dict[str, FakeSocialOAuthClient],
) -> None:
    connect = _create_connect_url(client, provider="x")

    response = client.post(
        "/v1/social/x/callback",
        json={"code": "provider-code", "state": connect["state"], "redirectUri": _REDIRECT_URI},
        headers=_auth_headers(_OTHER_USER_ID),
    )

    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason_code"] == "SOCIAL_AUTH_STATE_FORBIDDEN"
    assert fake_oauth_clients["x"].exchanges == []


async def test_disconnect_removes_connection(
    client: Any,
    db: Any,
    social_users: Any,
    fake_oauth_clients: dict[str, FakeSocialOAuthClient],
) -> None:
    connect = _create_connect_url(client, provider="x")
    callback = client.post(
        "/v1/social/x/callback",
        json={"code": "provider-code", "state": connect["state"], "redirectUri": _REDIRECT_URI},
        headers=_auth_headers(),
    )
    assert callback.status_code == 200, callback.text

    listed = client.get("/v1/social/connections", headers=_auth_headers())
    assert listed.status_code == 200
    x_connection = next(
        item for item in listed.json()["data"]["connections"] if item["provider"] == "x"
    )
    assert x_connection["connected"] is True

    response = client.delete("/v1/social/x", headers=_auth_headers())

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"provider": "x", "disconnected": True}
    async with db.session() as session:
        row = await session.scalar(
            select(SocialConnection).where(
                SocialConnection.user_id == _USER_ID,
                SocialConnection.provider == "x",
            )
        )
    assert row is None


def response_text(payload: dict[str, Any]) -> str:
    return str(payload)
