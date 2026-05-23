from __future__ import annotations

import pytest

from app.application.dto.social_capabilities import (
    default_social_scopes,
    get_social_provider_capabilities,
    list_social_provider_capabilities,
    unsupported_social_scopes,
)
from app.application.services.social_auth_service import (
    SocialAuthConfig,
    SocialAuthError,
    SocialAuthService,
)


class _FailingRepository:
    async def create_auth_state(self, _state):
        raise AssertionError("unsupported scopes should be rejected before persistence")


def test_social_provider_capability_snapshot() -> None:
    snapshot = {item.provider: item.to_dict() for item in list_social_provider_capabilities()}

    assert snapshot == {
        "instagram": {
            "provider": "instagram",
            "supports_single_url_lookup": True,
            "supports_owned_media_lookup": True,
            "supports_public_media_lookup": False,
            "supports_timeline_ingestion": False,
            "supports_refresh_tokens": True,
            "supported_scopes": ["instagram_business_basic"],
            "unsupported_notes": [
                "Authenticated Instagram API lookup is limited to media owned by the connected professional account.",
                "General public/private feed access, personal-account media access, publishing, comments, messaging, insights, and ads are intentionally unsupported.",
            ],
        },
        "threads": {
            "provider": "threads",
            "supports_single_url_lookup": True,
            "supports_owned_media_lookup": True,
            "supports_public_media_lookup": True,
            "supports_timeline_ingestion": True,
            "supports_refresh_tokens": True,
            "supported_scopes": ["threads_basic"],
            "unsupported_notes": [
                "Threads support is limited to read-only media lookup and the connected account's /me/threads feed.",
                "Publishing, replies, insights, and webhook behavior are intentionally unsupported.",
            ],
        },
        "x": {
            "provider": "x",
            "supports_single_url_lookup": True,
            "supports_owned_media_lookup": False,
            "supports_public_media_lookup": True,
            "supports_timeline_ingestion": True,
            "supports_refresh_tokens": True,
            "supported_scopes": ["tweet.read", "users.read", "offline.access"],
            "unsupported_notes": [
                "Read-only X API access is supported for post lookup and configured timeline ingestion.",
                "Write, publish, DM, moderation, and ad scopes are intentionally unsupported.",
            ],
        },
    }


def test_default_social_scopes_come_from_capabilities() -> None:
    assert default_social_scopes() == {
        "x": ["tweet.read", "users.read", "offline.access"],
        "instagram": ["instagram_business_basic"],
        "threads": ["threads_basic"],
    }


def test_unsupported_social_scopes_detects_write_scopes() -> None:
    assert unsupported_social_scopes(
        "instagram",
        ["instagram_business_basic", "instagram_business_content_publish"],
    ) == ["instagram_business_content_publish"]


def test_get_social_provider_capabilities_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported social provider"):
        get_social_provider_capabilities("mastodon")


@pytest.mark.asyncio
async def test_connect_url_scope_validation_rejects_unsupported_provider_scopes() -> None:
    service = SocialAuthService(
        repository=_FailingRepository(),
        oauth_clients={},
        config=SocialAuthConfig(provider_redirect_uris={"instagram": "https://example.test/cb"}),
    )

    with pytest.raises(SocialAuthError) as exc_info:
        await service.create_connect_url(
            user_id=1,
            provider="instagram",
            redirect_uri=None,
            scopes=["instagram_business_basic", "instagram_business_content_publish"],
        )

    assert exc_info.value.code == "SOCIAL_SCOPES_UNSUPPORTED"
    assert exc_info.value.details["unsupported_scopes"] == ["instagram_business_content_publish"]
    assert exc_info.value.details["supported_scopes"] == ["instagram_business_basic"]
