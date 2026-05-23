from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.adapters.social.meta import InstagramClient, InstagramOAuthConfig

_REDIRECT_URI = "https://app.example.com/social/instagram/callback"


def _client() -> InstagramClient:
    return InstagramClient(
        InstagramOAuthConfig(
            client_id="instagram-client-id",
            client_secret="instagram-client-secret",
            redirect_uri=_REDIRECT_URI,
            scopes=["instagram_business_basic"],
        )
    )


def test_authorization_url_uses_instagram_business_login_surface() -> None:
    url = _client().build_authorization_url(
        provider="instagram",
        state="state-123",
        code_challenge="unused-pkce",
        redirect_uri=_REDIRECT_URI,
        scopes=["instagram_business_basic"],
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert (
        f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        == "https://www.instagram.com/oauth/authorize"
    )
    assert query["client_id"] == ["instagram-client-id"]
    assert query["redirect_uri"] == [_REDIRECT_URI]
    assert query["scope"] == ["instagram_business_basic"]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["state-123"]


@pytest.mark.asyncio
async def test_exchange_code_gets_long_lived_token_and_profile(respx_mock) -> None:
    respx_mock.post("https://api.instagram.com/oauth/access_token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "short-token",
                "user_id": "17841400000000000",
                "permissions": "instagram_business_basic",
            },
        )
    )
    respx_mock.get("https://graph.instagram.com/access_token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "long-lived-token",
                "token_type": "bearer",
                "expires_in": 5184000,
            },
        )
    )
    me_route = respx_mock.get("https://graph.instagram.com/v25.0/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "app-scoped-id",
                "user_id": "17841400000000000",
                "username": "ig_user",
                "account_type": "Business",
                "media_count": 3,
            },
        )
    )

    result = await _client().exchange_code(
        provider="instagram",
        code="provider-code",
        redirect_uri=_REDIRECT_URI,
        code_verifier="unused",
        scopes=["instagram_business_basic"],
        correlation_id="cid",
    )

    assert result.access_token == "long-lived-token"
    assert result.refresh_token == "long-lived-token"
    assert result.provider_user_id == "17841400000000000"
    assert result.provider_username == "ig_user"
    assert result.scopes == ["instagram_business_basic"]
    assert result.metadata_json is not None
    assert result.metadata_json["instagram_token_kind"] == "long_lived"
    assert result.metadata_json["instagram_account"]["account_type"] == "Business"
    assert me_route.calls.last is not None
    assert (
        "instagram_business_content_publish" not in me_route.calls.last.request.url.params["fields"]
    )


@pytest.mark.asyncio
async def test_refresh_access_token_uses_instagram_refresh_endpoint(respx_mock) -> None:
    route = respx_mock.get("https://graph.instagram.com/refresh_access_token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "refreshed-token",
                "token_type": "bearer",
                "expires_in": 5184000,
            },
        )
    )

    result = await _client().refresh_access_token(
        provider="instagram",
        refresh_token="long-lived-token",
        scopes=["instagram_business_basic"],
        correlation_id="cid",
    )

    assert result.access_token == "refreshed-token"
    assert route.calls.last is not None
    query = route.calls.last.request.url.params
    assert query["grant_type"] == "ig_refresh_token"
    assert query["access_token"] == "long-lived-token"


@pytest.mark.asyncio
async def test_refresh_token_alias_uses_supported_refresh_endpoint(respx_mock) -> None:
    respx_mock.get("https://graph.instagram.com/refresh_access_token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "refreshed-token",
                "token_type": "bearer",
                "expires_in": 5184000,
            },
        )
    )

    result = await _client().refresh_token(refresh_token="long-lived-token")

    assert result.access_token == "refreshed-token"


@pytest.mark.asyncio
async def test_get_me_requests_professional_account_fields(respx_mock) -> None:
    route = respx_mock.get("https://graph.instagram.com/v25.0/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "user_id": "17841400000000000",
                "username": "ig_user",
                "account_type": "Media_Creator",
                "media_count": 4,
            },
        )
    )

    payload = await _client().get_me(access_token="long-lived-token")

    assert payload["account_type"] == "Media_Creator"
    assert route.calls.last is not None
    assert route.calls.last.request.url.params["access_token"] == "long-lived-token"
    fields = route.calls.last.request.url.params["fields"]
    assert "user_id" in fields
    assert "media_count" in fields


@pytest.mark.asyncio
async def test_get_media_by_id_normalizes_supported_media_fields(respx_mock) -> None:
    route = respx_mock.get("https://graph.instagram.com/v25.0/17918195224117851").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "17918195224117851",
                "caption": "Launch post",
                "media_type": "IMAGE",
                "media_url": "https://cdn.instagram.com/photo.jpg",
                "permalink": "https://www.instagram.com/p/shortcode/",
                "timestamp": "2026-05-23T10:00:00+0000",
                "username": "ig_user",
                "alt_text": "Chart",
            },
        )
    )

    media = await _client().get_media_by_id(
        "17918195224117851",
        access_token="long-lived-token",
    )

    assert media.id == "17918195224117851"
    assert media.caption == "Launch post"
    assert media.media_url == "https://cdn.instagram.com/photo.jpg"
    assert media.alt_text == "Chart"
    assert route.calls.last is not None
    assert route.calls.last.request.url.params["access_token"] == "long-lived-token"


@pytest.mark.asyncio
async def test_get_user_media_ids_uses_supported_user_media_edge(respx_mock) -> None:
    route = respx_mock.get("https://graph.instagram.com/v25.0/17841400000000000/media").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "17918195224117851"}, {"id": "17895695668004550"}],
                "paging": {"cursors": {"after": "cursor"}},
            },
        )
    )

    payload = await _client().get_user_media_ids(
        "17841400000000000",
        access_token="long-lived-token",
        limit=25,
    )

    assert payload["data"] == [{"id": "17918195224117851"}, {"id": "17895695668004550"}]
    assert payload["paging"]["cursors"]["after"] == "cursor"
    assert route.calls.last is not None
    assert route.calls.last.request.url.params["limit"] == "25"
