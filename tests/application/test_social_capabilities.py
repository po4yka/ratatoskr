from __future__ import annotations

import pytest

from app.application.ports.social_connections import (
    SocialAuthStateCreate,
    SocialAuthStateRecord,
    SocialConnectionRecord,
    SocialConnectionUpdate,
    SocialConnectionUpsert,
    SocialFetchAttemptCreate,
)
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
    async def get_by_user_and_provider(
        self, user_id: int, provider: str
    ) -> SocialConnectionRecord | None:
        raise AssertionError("connection lookup should not run for unsupported scopes")

    async def list_by_user(self, user_id: int) -> list[SocialConnectionRecord]:
        raise AssertionError("connection listing should not run for unsupported scopes")

    async def upsert_connection(self, connection: SocialConnectionUpsert) -> SocialConnectionRecord:
        raise AssertionError("connection upsert should not run for unsupported scopes")

    async def update_connection(
        self, user_id: int, provider: str, update: SocialConnectionUpdate
    ) -> SocialConnectionRecord | None:
        raise AssertionError("connection update should not run for unsupported scopes")

    async def delete_connection(self, user_id: int, provider: str) -> bool:
        raise AssertionError("connection delete should not run for unsupported scopes")

    async def create_auth_state(self, state: SocialAuthStateCreate) -> SocialAuthStateRecord:
        del state
        raise AssertionError("unsupported scopes should be rejected before persistence")

    async def get_auth_state(self, provider: str, state_hash: str) -> SocialAuthStateRecord | None:
        raise AssertionError("auth state lookup should not run for unsupported scopes")

    async def mark_auth_state_consumed(self, state_id: int) -> SocialAuthStateRecord | None:
        raise AssertionError("auth state consume should not run for unsupported scopes")

    async def mark_auth_state_expired(self, state_id: int) -> SocialAuthStateRecord | None:
        raise AssertionError("auth state expire should not run for unsupported scopes")

    async def record_fetch_attempt(self, attempt: SocialFetchAttemptCreate) -> None:
        raise AssertionError("fetch attempt recording should not run for unsupported scopes")


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
