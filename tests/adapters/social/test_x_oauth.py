from __future__ import annotations

import base64
import urllib.parse

import httpx
import pytest

from app.adapters.social.x import XOAuthClient, XOAuthConfig


def test_authorize_url_includes_state_and_s256_code_challenge() -> None:
    client = XOAuthClient(XOAuthConfig(client_id="client-123"))

    url = client.build_authorization_url(
        provider="x",
        state="state-abc",
        code_challenge="challenge-xyz",
        redirect_uri="https://app.example.com/callback",
        scopes=["tweet.read", "users.read", "offline.access"],
    )

    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://x.com/i/oauth2/authorize"
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-123"]
    assert query["redirect_uri"] == ["https://app.example.com/callback"]
    assert query["scope"] == ["tweet.read users.read offline.access"]
    assert query["state"] == ["state-abc"]
    assert query["code_challenge"] == ["challenge-xyz"]
    assert query["code_challenge_method"] == ["S256"]


@pytest.mark.asyncio
async def test_exchange_code_posts_pkce_form_and_fetches_user(respx_mock) -> None:
    token_route = respx_mock.post("https://api.x.com/2/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "token_type": "bearer",
                "access_token": "x-access-token",
                "refresh_token": "x-refresh-token",
                "expires_in": 7200,
                "scope": "tweet.read users.read offline.access",
            },
        )
    )
    me_route = respx_mock.get("https://api.x.com/2/users/me").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"id": "42", "username": "x_user"}},
        )
    )
    client = XOAuthClient(XOAuthConfig(client_id="client-123"))

    result = await client.exchange_code(
        provider="x",
        code="code-secret",
        redirect_uri="https://app.example.com/callback",
        code_verifier="verifier-secret",
        scopes=["tweet.read", "users.read", "offline.access"],
        correlation_id="cid-x-test",
    )

    request = token_route.calls[0].request
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    form = urllib.parse.parse_qs(request.content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["code-secret"]
    assert form["redirect_uri"] == ["https://app.example.com/callback"]
    assert form["code_verifier"] == ["verifier-secret"]
    assert form["client_id"] == ["client-123"]
    assert me_route.calls[0].request.headers["authorization"] == "Bearer x-access-token"
    assert result.access_token == "x-access-token"
    assert result.refresh_token == "x-refresh-token"
    assert result.provider_user_id == "42"
    assert result.provider_username == "x_user"
    assert result.scopes == ["tweet.read", "users.read", "offline.access"]
    assert result.access_token_expires_at is not None


@pytest.mark.asyncio
async def test_refresh_uses_refresh_grant_and_rotates_token(respx_mock) -> None:
    route = respx_mock.post("https://api.x.com/2/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "token_type": "bearer",
                "access_token": "rotated-access-token",
                "refresh_token": "rotated-refresh-token",
                "expires_in": 7200,
                "scope": "tweet.read users.read offline.access",
            },
        )
    )
    client = XOAuthClient(XOAuthConfig(client_id="client-123"))

    result = await client.refresh_access_token(
        provider="x",
        refresh_token="old-refresh-token",
        scopes=["tweet.read", "users.read", "offline.access"],
        correlation_id="cid-x-test",
    )

    form = urllib.parse.parse_qs(route.calls[0].request.content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == ["old-refresh-token"]
    assert form["client_id"] == ["client-123"]
    assert result.access_token == "rotated-access-token"
    assert result.refresh_token == "rotated-refresh-token"


@pytest.mark.asyncio
async def test_confidential_client_uses_basic_auth_without_client_id_body(respx_mock) -> None:
    route = respx_mock.post("https://api.x.com/2/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "access", "expires_in": 7200})
    )
    client = XOAuthClient(XOAuthConfig(client_id="client-123", client_secret="secret-456"))

    await client.refresh_access_token(
        provider="x",
        refresh_token="refresh-token",
        scopes=["tweet.read"],
        correlation_id=None,
    )

    request = route.calls[0].request
    expected = base64.b64encode(b"client-123:secret-456").decode("ascii")
    assert request.headers["authorization"] == f"Basic {expected}"
    form = urllib.parse.parse_qs(request.content.decode())
    assert "client_id" not in form
