from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.adapters.content.platform_extraction.lifecycle import PlatformRequestLifecycle
from app.adapters.content.platform_extraction.models import PlatformExtractionRequest
from app.adapters.twitter.api_extractor import XApiExtractionResult, XApiPostExtractor
from app.adapters.twitter.extraction_coordinator import TwitterExtractionCoordinator
from app.adapters.twitter.firecrawl_extractor import TwitterFirecrawlExtractor
from app.adapters.twitter.playwright_extractor import TwitterPlaywrightExtractor
from app.adapters.twitter.tier_policy import TwitterTierPolicy


class FakeTierPolicy(TwitterTierPolicy):
    def __init__(self, *, firecrawl: bool = True, playwright: bool = True) -> None:
        self._firecrawl = firecrawl
        self._playwright = playwright

    def force_tier(self) -> str:
        return "auto"

    def should_use_firecrawl_tier(self) -> bool:
        return self._firecrawl

    def should_use_playwright_tier(self) -> bool:
        return self._playwright

    def effective_timeout_ms(self) -> int:
        return 15000

    def build_extraction_error_message(self) -> str:
        return "Twitter content extraction failed"


class _FakeLifecycle(PlatformRequestLifecycle):
    def __init__(self) -> None:
        return None

    async def send_accepted_notification(self, request: Any) -> None:
        return None

    async def handle_request_dedupe_or_create(
        self,
        request: Any,
        *,
        dedupe_hash: str,
        paper_canonical_id: str | None = None,
    ) -> int:
        del paper_canonical_id
        return 1

    async def persist_detected_lang(self, request_id: int, lang: str) -> None:
        return None


class _FakeXApiPostExtractor(XApiPostExtractor):
    extract: AsyncMock

    def __init__(self, result: XApiExtractionResult) -> None:
        self.extract = AsyncMock(return_value=result)


class _FakeTwitterFirecrawlExtractor(TwitterFirecrawlExtractor):
    extract: AsyncMock

    def __init__(self) -> None:
        self.extract = AsyncMock(return_value=(True, "firecrawl body", "markdown"))


class _FakeTwitterPlaywrightExtractor(TwitterPlaywrightExtractor):
    extract: AsyncMock

    def __init__(self) -> None:
        self.extract = AsyncMock(return_value=("playwright body", "twitter_graphql", {}))


def _cfg() -> Any:
    return SimpleNamespace(
        runtime=SimpleNamespace(aggregation_article_media_enabled=True),
        twitter=SimpleNamespace(
            prefer_firecrawl=True,
            playwright_enabled=True,
            article_redirect_resolution_enabled=True,
            article_resolution_timeout_sec=5.0,
        ),
    )


def _request(*, user_id: int | None = 777) -> PlatformExtractionRequest:
    return PlatformExtractionRequest(
        message=None,
        url_text="https://x.com/example/status/123",
        normalized_url="https://x.com/example/status/123",
        correlation_id="cid",
        request_id_override=99,
        mode="pure",
        user_id=user_id,
    )


def _coordinator(
    *,
    api_result: XApiExtractionResult | None,
    firecrawl_enabled: bool = True,
    playwright_enabled: bool = True,
) -> tuple[Any, Any, Any, Any]:
    api = _FakeXApiPostExtractor(api_result) if api_result else None
    firecrawl = _FakeTwitterFirecrawlExtractor()
    playwright = _FakeTwitterPlaywrightExtractor()
    coordinator = TwitterExtractionCoordinator(
        cfg=_cfg(),
        response_formatter=SimpleNamespace(send_error_notification=AsyncMock()),
        request_repo=SimpleNamespace(),
        lifecycle=_FakeLifecycle(),
        tier_policy=FakeTierPolicy(firecrawl=firecrawl_enabled, playwright=playwright_enabled),
        x_api_extractor=api,
        firecrawl_extractor=firecrawl,
        playwright_extractor=playwright,
    )
    return coordinator, api, firecrawl, playwright


@pytest.mark.asyncio
async def test_no_connection_continues_to_firecrawl_path() -> None:
    coordinator, api, firecrawl, playwright = _coordinator(
        api_result=XApiExtractionResult(
            ok=False,
            metadata={"api_status": "no_connection", "provider_resource_id": "123"},
        ),
        playwright_enabled=False,
    )

    result = await coordinator.extract(_request(user_id=777))

    assert result.content_text == "firecrawl body"
    assert result.metadata["auth_strategy"]["selected_tier"] == "firecrawl"
    assert result.metadata["tier_outcomes"]["x_api"] == "skipped"
    api.extract.assert_awaited_once()
    firecrawl.extract.assert_awaited_once()
    playwright.extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_connection_uses_x_api_before_fallback_tiers() -> None:
    coordinator, api, firecrawl, playwright = _coordinator(
        api_result=XApiExtractionResult(
            ok=True,
            content_text="api body",
            content_source="x_api",
            metadata={
                "auth_strategy": {"selected_tier": "x_api"},
                "api_status": "ok",
                "provider_resource_id": "123",
                "tweet_media": [
                    {"url": "https://pbs.twimg.com/media/photo.jpg", "alt_text": "Chart"}
                ],
            },
        )
    )

    result = await coordinator.extract(_request(user_id=777))

    assert result.content_text == "api body"
    assert result.content_source == "x_api"
    assert result.metadata["auth_strategy"]["selected_tier"] == "x_api"
    assert result.normalized_document is not None
    assert result.normalized_document.media[0].url == "https://pbs.twimg.com/media/photo.jpg"
    api.extract.assert_awaited_once()
    firecrawl.extract.assert_not_awaited()
    playwright.extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_x_api_success_works_even_when_fallback_tiers_disabled() -> None:
    coordinator, _api, firecrawl, playwright = _coordinator(
        api_result=XApiExtractionResult(
            ok=True,
            content_text="api body",
            content_source="x_api",
            metadata={
                "auth_strategy": {"selected_tier": "x_api"},
                "api_status": "ok",
                "provider_resource_id": "123",
            },
        ),
        firecrawl_enabled=False,
        playwright_enabled=False,
    )

    result = await coordinator.extract(_request(user_id=777))

    assert result.content_text == "api body"
    assert result.metadata["auth_strategy"]["selected_tier"] == "x_api"
    firecrawl.extract.assert_not_awaited()
    playwright.extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_401_metadata_falls_back_to_firecrawl() -> None:
    coordinator, api, firecrawl, _playwright = _coordinator(
        api_result=XApiExtractionResult(
            ok=False,
            metadata={
                "auth_strategy": {"selected_tier": "x_api"},
                "api_status": "401",
                "provider_resource_id": "123",
            },
        ),
        playwright_enabled=False,
    )

    result = await coordinator.extract(_request(user_id=777))

    assert result.content_text == "firecrawl body"
    assert result.metadata["api_status"] == "401"
    assert result.metadata["provider_resource_id"] == "123"
    assert result.metadata["auth_strategy"]["selected_tier"] == "firecrawl"
    api.extract.assert_awaited_once()
    firecrawl.extract.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_429_rate_limit_metadata_falls_back_to_firecrawl() -> None:
    coordinator, _api, firecrawl, _playwright = _coordinator(
        api_result=XApiExtractionResult(
            ok=False,
            metadata={
                "auth_strategy": {"selected_tier": "x_api"},
                "api_status": "429",
                "rate_limit": {"reset": "1779519999"},
                "provider_resource_id": "123",
            },
        ),
        playwright_enabled=False,
    )

    result = await coordinator.extract(_request(user_id=777))

    assert result.content_text == "firecrawl body"
    assert result.metadata["api_status"] == "429"
    assert result.metadata["rate_limit"]["reset"] == "1779519999"
    assert result.metadata["auth_strategy"]["selected_tier"] == "firecrawl"
    firecrawl.extract.assert_awaited_once()
