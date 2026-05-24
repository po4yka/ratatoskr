"""DI builders for social OAuth and connected-token services."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.social.meta import (
    InstagramClient,
    InstagramOAuthConfig,
    ThreadsClient,
    ThreadsOAuthConfig,
)
from app.adapters.social.x import XOAuthClient, XOAuthConfig
from app.application.services.social_auth_service import (
    DEFAULT_SOCIAL_SCOPES,
    SocialAuthConfig,
    SocialAuthService,
    build_stub_social_oauth_clients,
)
from app.application.services.social_token_service import SocialAccessTokenResolver

if TYPE_CHECKING:
    from app.application.ports.social_connections import SocialConnectionRepositoryPort
    from app.config import AppConfig


def build_social_oauth_clients(cfg: AppConfig) -> dict[str, Any]:
    """Build configured provider OAuth clients."""
    clients = build_stub_social_oauth_clients()
    twitter_cfg = cfg.twitter
    social_cfg = cfg.social
    clients["x"] = XOAuthClient(
        XOAuthConfig(
            client_id=twitter_cfg.x_oauth_client_id,
            client_secret=twitter_cfg.x_oauth_client_secret.get_secret_value()
            if twitter_cfg.x_oauth_client_secret is not None
            else None,
            redirect_uri=twitter_cfg.x_oauth_redirect_uri,
            scopes=twitter_cfg.x_oauth_scopes,
            api_base_url=twitter_cfg.x_api_base_url,
        )
    )
    clients["threads"] = ThreadsClient(
        ThreadsOAuthConfig(
            client_id=social_cfg.threads_client_id,
            client_secret=social_cfg.threads_client_secret.get_secret_value()
            if social_cfg.threads_client_secret is not None
            else None,
            redirect_uri=social_cfg.threads_redirect_uri,
            scopes=social_cfg.threads_scopes,
            graph_base_url=social_cfg.threads_graph_base_url,
        )
    )
    clients["instagram"] = InstagramClient(
        InstagramOAuthConfig(
            client_id=social_cfg.instagram_client_id,
            client_secret=social_cfg.instagram_client_secret.get_secret_value()
            if social_cfg.instagram_client_secret is not None
            else None,
            redirect_uri=social_cfg.instagram_redirect_uri,
            scopes=social_cfg.instagram_scopes,
            graph_base_url=social_cfg.instagram_graph_base_url,
        )
    )
    return clients


def build_social_auth_service(
    cfg: AppConfig,
    social_connection_repository: SocialConnectionRepositoryPort,
) -> SocialAuthService:
    """Build provider-neutral social auth service from runtime dependencies."""
    twitter_cfg = cfg.twitter
    social_cfg = cfg.social
    return SocialAuthService(
        repository=social_connection_repository,
        oauth_clients=build_social_oauth_clients(cfg),
        config=SocialAuthConfig(
            provider_default_scopes={
                **DEFAULT_SOCIAL_SCOPES,
                "x": twitter_cfg.x_oauth_scopes,
                "threads": social_cfg.threads_scopes,
                "instagram": social_cfg.instagram_scopes,
            },
            provider_redirect_uris={
                "x": twitter_cfg.x_oauth_redirect_uri,
                "threads": social_cfg.threads_redirect_uri,
                "instagram": social_cfg.instagram_redirect_uri,
            },
        ),
    )


def build_social_token_resolver(
    cfg: AppConfig,
    social_connection_repository: SocialConnectionRepositoryPort,
) -> SocialAccessTokenResolver:
    """Build shared connected-account access-token resolver."""
    return SocialAccessTokenResolver(
        repository=social_connection_repository,
        oauth_clients=build_social_oauth_clients(cfg),
    )
