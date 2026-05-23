from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from app.adapters.content.platform_extraction.models import PlatformExtractionRequest
from app.adapters.meta.instagram_api_extractor import (
    InstagramApiExtractionResult,
    InstagramApiExtractor,
)
from app.adapters.meta.platform_extractor import MetaPlatformExtractor
from app.adapters.social.meta import InstagramOAuthError
from app.application.dto.social_auth import OAuthTokenResult
from app.application.ports.social_connections import (
    SocialConnectionRecord,
    SocialConnectionUpdate,
    SocialFetchAttemptCreate,
)
from app.core.time_utils import UTC
from app.domain.models.source import SourceKind
from app.security.secret_crypto import encrypt_secret, reset_secret_key_cache


class FakeSocialConnectionRepository:
    def __init__(self, connection: SocialConnectionRecord | None) -> None:
        self.connection = connection
        self.attempts: list[SocialFetchAttemptCreate] = []
        self.updates: list[SocialConnectionUpdate] = []

    async def get_by_user_and_provider(
        self, user_id: int, provider: str
    ) -> SocialConnectionRecord | None:
        if (
            self.connection
            and self.connection.user_id == user_id
            and self.connection.provider == provider
        ):
            return self.connection
        return None

    async def update_connection(
        self, user_id: int, provider: str, update: SocialConnectionUpdate
    ) -> SocialConnectionRecord | None:
        assert self.connection is not None
        assert self.connection.user_id == user_id
        assert self.connection.provider == provider
        self.updates.append(update)
        self.connection = replace(
            self.connection,
            encrypted_access_token=update.encrypted_access_token
            if update.encrypted_access_token is not None
            else self.connection.encrypted_access_token,
            encrypted_refresh_token=update.encrypted_refresh_token
            if update.encrypted_refresh_token is not None
            else self.connection.encrypted_refresh_token,
            token_scopes=update.token_scopes
            if update.token_scopes is not None
            else self.connection.token_scopes,
            access_token_expires_at=update.access_token_expires_at
            if update.access_token_expires_at is not None
            else self.connection.access_token_expires_at,
            refresh_token_expires_at=update.refresh_token_expires_at
            if update.refresh_token_expires_at is not None
            else self.connection.refresh_token_expires_at,
            status=update.status if update.status is not None else self.connection.status,
            metadata_json=update.metadata_json
            if update.metadata_json is not None
            else self.connection.metadata_json,
        )
        return self.connection

    async def record_fetch_attempt(self, attempt: SocialFetchAttemptCreate) -> None:
        self.attempts.append(attempt)


class FakeInstagramClient:
    def __init__(
        self,
        *,
        media_pages: list[dict[str, Any]] | None = None,
        media_payloads: dict[str, dict[str, Any]] | None = None,
        error: InstagramOAuthError | None = None,
    ) -> None:
        self.media_pages = media_pages or []
        self.media_payloads = media_payloads or {}
        self.error = error
        self.media_page_calls: list[dict[str, Any]] = []
        self.media_payload_calls: list[str] = []

    async def get_user_media_ids(
        self,
        ig_user_id: str,
        *,
        access_token: str,
        limit: int | None = None,
        before: str | None = None,
        after: str | None = None,
    ) -> dict[str, Any]:
        del access_token, before
        if self.error is not None:
            raise self.error
        self.media_page_calls.append({"ig_user_id": ig_user_id, "limit": limit, "after": after})
        if not self.media_pages:
            return {"data": []}
        return self.media_pages[min(len(self.media_page_calls) - 1, len(self.media_pages) - 1)]

    async def get_media_payload(self, media_id: str, *, access_token: str) -> dict[str, Any]:
        del access_token
        if self.error is not None:
            raise self.error
        self.media_payload_calls.append(media_id)
        return self.media_payloads[media_id]

    async def get_me(self, *, access_token: str) -> dict[str, Any]:
        del access_token
        return {"user_id": "17841400000000000", "username": "ig_user"}

    async def refresh_token(self, *, refresh_token: str) -> OAuthTokenResult:
        del refresh_token
        return OAuthTokenResult(
            access_token="new-access",
            refresh_token="new-refresh",
            scopes=["instagram_business_basic"],
            access_token_expires_at=(dt.datetime.now(UTC) + dt.timedelta(hours=1)).isoformat(),
        )


class _DummySemCtx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self, exc_type: object | None, exc: BaseException | None, tb: object | None
    ) -> bool:
        return False


@pytest.fixture(autouse=True)
def _crypto_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    reset_secret_key_cache()
    yield
    reset_secret_key_cache()


def _connection(*, status: str = "active") -> SocialConnectionRecord:
    now = dt.datetime.now(UTC)
    return SocialConnectionRecord(
        id=10,
        user_id=777,
        provider="instagram",
        auth_type="oauth2",
        provider_user_id="17841400000000000",
        provider_username="ig_user",
        encrypted_access_token=encrypt_secret("old-access"),
        encrypted_refresh_token=encrypt_secret("old-refresh"),
        token_scopes=["instagram_business_basic"],
        access_token_expires_at=now + dt.timedelta(hours=1),
        refresh_token_expires_at=None,
        status=status,
        metadata_json={},
        created_at=now,
        updated_at=now,
    )


def _instagram_request() -> PlatformExtractionRequest:
    return PlatformExtractionRequest(
        message=None,
        url_text="https://www.instagram.com/p/ABC123/",
        normalized_url="https://www.instagram.com/p/ABC123/",
        correlation_id="cid",
        request_id_override=99,
        mode="pure",
        user_id=777,
    )


@pytest.mark.asyncio
async def test_supported_connected_media_path_maps_official_media_document() -> None:
    repo = FakeSocialConnectionRepository(_connection())
    client = FakeInstagramClient(
        media_pages=[{"data": [{"id": "1791"}]}],
        media_payloads={
            "1791": {
                "id": "1791",
                "caption": "Official API caption",
                "media_type": "IMAGE",
                "media_url": "https://cdn.instagram.com/photo.jpg",
                "permalink": "https://www.instagram.com/p/ABC123/",
                "timestamp": "2026-05-23T10:00:00+0000",
                "username": "ig_user",
                "alt_text": "Chart",
            }
        },
    )
    extractor = InstagramApiExtractor(repository=repo, instagram_client=client)

    result = await extractor.extract(
        url="https://www.instagram.com/p/ABC123/",
        kind_hint=SourceKind.INSTAGRAM_POST,
        user_id=777,
        request_id=99,
        dedupe_hash="dedupe",
    )

    assert result.ok is True
    assert result.content_source == "instagram_api"
    assert "Official API caption" in result.content_text
    assert result.metadata is not None
    assert result.metadata["auth_strategy"]["selected_tier"] == "instagram_api"
    assert result.metadata["api_supported_for_url"] is True
    assert result.metadata["provider_resource_id"] == "1791"
    assert result.source_item is not None
    assert result.source_item.kind == SourceKind.INSTAGRAM_POST
    assert result.normalized_document is not None
    assert result.normalized_document.media[0].url == "https://cdn.instagram.com/photo.jpg"
    assert client.media_page_calls[0]["ig_user_id"] == "17841400000000000"
    assert repo.attempts[0].status == "succeeded"
    assert repo.attempts[0].metadata_json is not None
    assert "instagram_media" not in repo.attempts[0].metadata_json


@pytest.mark.asyncio
async def test_connected_media_resolution_rejects_non_owned_shortcode() -> None:
    repo = FakeSocialConnectionRepository(_connection())
    client = FakeInstagramClient(
        media_pages=[{"data": [{"id": "1791"}]}],
        media_payloads={
            "1791": {
                "id": "1791",
                "caption": "Different post",
                "media_type": "IMAGE",
                "media_url": "https://cdn.instagram.com/photo.jpg",
                "permalink": "https://www.instagram.com/p/DIFFERENT/",
            }
        },
    )
    extractor = InstagramApiExtractor(repository=repo, instagram_client=client)

    result = await extractor.extract(
        url="https://www.instagram.com/p/ABC123/",
        kind_hint=SourceKind.INSTAGRAM_POST,
        user_id=777,
        request_id=99,
        dedupe_hash="dedupe",
    )

    assert result.ok is False
    assert result.metadata is not None
    assert result.metadata["api_supported_for_url"] is False
    assert result.metadata["unsupported_reason"] == "not_connected_account_media"
    assert repo.attempts[0].status == "failed"
    assert repo.attempts[0].error_code == "unsupported"


@pytest.mark.asyncio
async def test_supported_carousel_media_maps_to_carousel_source_kind() -> None:
    repo = FakeSocialConnectionRepository(_connection())
    client = FakeInstagramClient(
        media_pages=[{"data": [{"id": "1792"}]}],
        media_payloads={
            "1792": {
                "id": "1792",
                "caption": "Carousel caption",
                "media_type": "CAROUSEL_ALBUM",
                "permalink": "https://www.instagram.com/p/ABC123/",
                "children": {
                    "data": [
                        {
                            "id": "child-1",
                            "media_type": "IMAGE",
                            "media_url": "https://cdn.instagram.com/child.jpg",
                        }
                    ]
                },
            }
        },
    )
    extractor = InstagramApiExtractor(repository=repo, instagram_client=client)

    result = await extractor.extract(
        url="https://www.instagram.com/p/ABC123/",
        kind_hint=SourceKind.INSTAGRAM_POST,
        user_id=777,
        request_id=99,
        dedupe_hash="dedupe",
    )

    assert result.ok is True
    assert result.source_item is not None
    assert result.source_item.kind == SourceKind.INSTAGRAM_CAROUSEL
    assert result.normalized_document is not None
    assert result.normalized_document.source_kind == SourceKind.INSTAGRAM_CAROUSEL
    assert result.normalized_document.media[0].url == "https://cdn.instagram.com/child.jpg"


@pytest.mark.asyncio
async def test_supported_reel_url_maps_to_reel_source_kind() -> None:
    repo = FakeSocialConnectionRepository(_connection())
    client = FakeInstagramClient(
        media_pages=[{"data": [{"id": "1793"}]}],
        media_payloads={
            "1793": {
                "id": "1793",
                "caption": "Reel caption",
                "media_type": "VIDEO",
                "media_url": "https://cdn.instagram.com/reel.mp4",
                "permalink": "https://www.instagram.com/reel/ABC123/",
                "thumbnail_url": "https://cdn.instagram.com/reel.jpg",
            }
        },
    )
    extractor = InstagramApiExtractor(repository=repo, instagram_client=client)

    result = await extractor.extract(
        url="https://www.instagram.com/reel/ABC123/",
        kind_hint=SourceKind.INSTAGRAM_REEL,
        user_id=777,
        request_id=99,
        dedupe_hash="dedupe",
    )

    assert result.ok is True
    assert result.source_item is not None
    assert result.source_item.kind == SourceKind.INSTAGRAM_REEL
    assert result.normalized_document is not None
    assert result.normalized_document.source_kind == SourceKind.INSTAGRAM_REEL
    assert any(asset.kind.value == "video" for asset in result.normalized_document.media)


@pytest.mark.asyncio
async def test_unsupported_public_url_path_falls_back_to_scraper_with_metadata() -> None:
    scraper = SimpleNamespace(
        scrape_markdown=AsyncMock(
            return_value=SimpleNamespace(
                status="ok",
                content_markdown="Fallback scraper caption",
                content_html=None,
                metadata_json={"title": "Instagram post"},
            )
        )
    )
    instagram_api = SimpleNamespace(
        extract=AsyncMock(
            return_value=InstagramApiExtractionResult(
                ok=False,
                metadata={
                    "api_status": "unsupported",
                    "api_supported_for_url": False,
                    "unsupported_reason": "not_connected_account_media",
                    "provider_shortcode": "ABC123",
                    "auth_strategy": {
                        "authenticated_supported": True,
                        "selected_tier": "meta_scraper_fallback",
                    },
                },
            )
        )
    )
    extractor = MetaPlatformExtractor(
        cfg=SimpleNamespace(runtime=SimpleNamespace(aggregation_non_youtube_video_enabled=True)),
        scraper=scraper,
        firecrawl_sem=lambda: _DummySemCtx(),
        lifecycle=SimpleNamespace(
            send_accepted_notification=AsyncMock(),
            handle_request_dedupe_or_create=AsyncMock(return_value=1),
            persist_detected_lang=AsyncMock(),
        ),
        instagram_api_extractor=instagram_api,
    )

    result = await extractor.extract(_instagram_request())

    assert result.content_text == "Fallback scraper caption"
    assert result.content_source == "markdown"
    assert result.metadata["api_supported_for_url"] is False
    assert result.metadata["unsupported_reason"] == "not_connected_account_media"
    assert result.metadata["auth_strategy"]["authenticated_supported"] is True
    assert result.metadata["auth_strategy"]["selected_tier"] == "meta_scraper_fallback"
    instagram_api.extract.assert_awaited_once()
    scraper.scrape_markdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_metadata_fallback_selected_when_scraper_has_only_metadata_after_api_unsupported() -> (
    None
):
    scraper = SimpleNamespace(
        scrape_markdown=AsyncMock(
            return_value=SimpleNamespace(
                status="ok",
                content_markdown="Log in to Instagram to continue",
                content_html=None,
                metadata_json={"description": "Metadata caption"},
            )
        )
    )
    instagram_api = SimpleNamespace(
        extract=AsyncMock(
            return_value=InstagramApiExtractionResult(
                ok=False,
                metadata={
                    "api_status": "unsupported",
                    "api_supported_for_url": False,
                    "unsupported_reason": "not_connected_account_media",
                    "auth_strategy": {
                        "authenticated_supported": True,
                        "selected_tier": "meta_scraper_fallback",
                    },
                },
            )
        )
    )
    extractor = MetaPlatformExtractor(
        cfg=SimpleNamespace(runtime=SimpleNamespace(aggregation_non_youtube_video_enabled=True)),
        scraper=scraper,
        firecrawl_sem=lambda: _DummySemCtx(),
        lifecycle=SimpleNamespace(
            send_accepted_notification=AsyncMock(),
            handle_request_dedupe_or_create=AsyncMock(return_value=1),
            persist_detected_lang=AsyncMock(),
        ),
        instagram_api_extractor=instagram_api,
    )

    result = await extractor.extract(_instagram_request())

    assert result.content_source == "meta_metadata_fallback"
    assert result.metadata["auth_strategy"]["selected_tier"] == "metadata_fallback"


@pytest.mark.asyncio
async def test_token_failure_marks_needs_reauth_and_records_failed_attempt() -> None:
    repo = FakeSocialConnectionRepository(_connection())
    client = FakeInstagramClient(
        error=InstagramOAuthError(
            "rejected",
            code="INSTAGRAM_GRAPH_REQUEST_REJECTED",
            status_code=401,
        )
    )
    extractor = InstagramApiExtractor(repository=repo, instagram_client=client)

    result = await extractor.extract(
        url="https://www.instagram.com/p/ABC123/",
        kind_hint=SourceKind.INSTAGRAM_POST,
        user_id=777,
        request_id=99,
        dedupe_hash="dedupe",
    )

    assert result.ok is False
    assert result.metadata is not None
    assert result.metadata["api_status"] == "401"
    assert result.metadata["unsupported_reason"] == "token_invalid"
    assert repo.connection is not None
    assert repo.connection.status == "needs_reauth"
    assert repo.attempts[0].status == "failed"
    assert repo.attempts[0].error_code == "unauthorized"
