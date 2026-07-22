from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import SecretStr

from app.adapters.social.meta import InstagramClient, ThreadsClient
from app.adapters.social.x import XOAuthClient
from app.application.services.social_auth_service import (
    SocialAuthError,
    SocialAuthService,
    StubSocialOAuthClient,
)
from app.application.services.social_token_service import SocialAccessTokenResolver
from app.config import SocialConfig, TwitterConfig
from app.di.social import (
    build_social_auth_service,
    build_social_oauth_clients,
    build_social_token_resolver,
)
from tests.conftest import make_test_app_config


class _FakeSocialConnectionRepository:
    pass


def test_build_social_oauth_clients_uses_app_config_values() -> None:
    cfg = make_test_app_config(
        twitter=TwitterConfig(
            x_oauth_client_id="x-client-id",
            x_oauth_client_secret=SecretStr("x-client-secret"),
            x_oauth_redirect_uri="https://app.example.com/social/x/callback",
            x_oauth_scopes=["tweet.read", "users.read", "offline.access"],
            x_api_base_url="https://x-api.example.test/2",
        ),
        social=SocialConfig(
            threads_client_id="threads-client-id",
            threads_client_secret=SecretStr("threads-client-secret"),
            threads_redirect_uri="https://app.example.com/social/threads/callback",
            threads_scopes=["threads_basic"],
            threads_graph_base_url="https://threads-graph.example.test/v1.0",
            instagram_client_id="instagram-client-id",
            instagram_client_secret=SecretStr("instagram-client-secret"),
            instagram_redirect_uri="https://app.example.com/social/instagram/callback",
            instagram_scopes=["instagram_business_basic"],
            instagram_graph_base_url="https://instagram-graph.example.test/v25.0",
        ),
    )

    clients = build_social_oauth_clients(cfg)

    assert isinstance(clients["x"], XOAuthClient)
    assert isinstance(clients["threads"], ThreadsClient)
    assert isinstance(clients["instagram"], InstagramClient)
    assert _query(clients["x"])["client_id"] == ["x-client-id"]
    assert _query(clients["x"])["redirect_uri"] == ["https://app.example.com/social/x/callback"]
    assert _query(clients["threads"])["client_id"] == ["threads-client-id"]
    assert _query(clients["threads"])["redirect_uri"] == [
        "https://app.example.com/social/threads/callback"
    ]
    assert _query(clients["instagram"])["client_id"] == ["instagram-client-id"]
    assert _query(clients["instagram"])["redirect_uri"] == [
        "https://app.example.com/social/instagram/callback"
    ]


def test_build_social_oauth_clients_falls_back_to_stub_when_unconfigured() -> None:
    cfg = make_test_app_config()

    clients = build_social_oauth_clients(cfg)

    assert isinstance(clients["x"], StubSocialOAuthClient)
    assert isinstance(clients["threads"], StubSocialOAuthClient)
    assert isinstance(clients["instagram"], StubSocialOAuthClient)


def test_build_social_oauth_clients_gates_threads_and_instagram_on_secret() -> None:
    """Threads/Instagram are confidential clients -- a client ID alone isn't enough."""
    cfg = make_test_app_config(
        social=SocialConfig(
            threads_client_id="threads-client-id",
            instagram_client_id="instagram-client-id",
        ),
    )

    clients = build_social_oauth_clients(cfg)

    assert isinstance(clients["threads"], StubSocialOAuthClient)
    assert isinstance(clients["instagram"], StubSocialOAuthClient)


def test_build_social_oauth_clients_gates_x_on_client_id_only() -> None:
    """X supports Authorization Code with PKCE for public clients -- no secret required."""
    cfg = make_test_app_config(
        twitter=TwitterConfig(x_oauth_client_id="x-client-id"),
    )

    clients = build_social_oauth_clients(cfg)

    assert isinstance(clients["x"], XOAuthClient)
    assert isinstance(clients["threads"], StubSocialOAuthClient)
    assert isinstance(clients["instagram"], StubSocialOAuthClient)


async def test_unconfigured_provider_reports_clean_501_not_a_url_build_failure() -> None:
    """An unconfigured provider must fail with the stub's clean 501, not a 502.

    Regression guard: wiring a real client unconditionally would raise the
    provider-specific "client not configured" error straight out of
    `build_authorization_url`, which `SocialAuthService.create_connect_url`
    could only catch as a generic `Exception` and re-wrap as a confusing 502
    `SOCIAL_AUTHORIZATION_URL_FAILED`.
    """
    cfg = make_test_app_config()
    clients = build_social_oauth_clients(cfg)

    with pytest.raises(SocialAuthError) as exc_info:
        await clients["threads"].exchange_code(
            provider="threads",
            code="code",
            redirect_uri="https://app.example.com/social/threads/callback",
            code_verifier="verifier",
            scopes=["threads_basic"],
            correlation_id=None,
        )

    assert exc_info.value.code == "SOCIAL_OAUTH_CLIENT_NOT_CONFIGURED"
    assert exc_info.value.status_code == 501


def test_social_di_builds_provider_neutral_services() -> None:
    cfg = make_test_app_config()
    repository = _FakeSocialConnectionRepository()

    auth_service = build_social_auth_service(cfg, repository)  # type: ignore[arg-type]
    token_resolver = build_social_token_resolver(cfg, repository)  # type: ignore[arg-type]

    assert isinstance(auth_service, SocialAuthService)
    assert isinstance(token_resolver, SocialAccessTokenResolver)


def _query(client: Any) -> dict[str, list[str]]:
    url = client.build_authorization_url(
        provider="provider",
        state="state",
        code_challenge="challenge",
        redirect_uri=client._config.redirect_uri,
        scopes=list(client._config.default_scopes),
    )
    return parse_qs(urlparse(url).query)
