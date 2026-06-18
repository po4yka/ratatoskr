from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.adapters.content.platform_extraction.lifecycle import PlatformRequestLifecycle
from app.adapters.content.platform_extraction.models import PlatformExtractionRequest
from app.adapters.meta.platform_extractor import MetaPlatformExtractor
from app.adapters.meta.threads_api_extractor import ThreadsApiExtractionResult, ThreadsApiExtractor


class _DummySemCtx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self, exc_type: object | None, exc: BaseException | None, tb: object | None
    ) -> bool:
        return False


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


class _FakeThreadsApiExtractor(ThreadsApiExtractor):
    extract: AsyncMock

    def __init__(self, result: ThreadsApiExtractionResult) -> None:
        self.extract = AsyncMock(return_value=result)


def _make_request(*, user_id: int | None = 777) -> PlatformExtractionRequest:
    return PlatformExtractionRequest(
        message=None,
        url_text="https://www.threads.net/@user/post/C8abc123",
        normalized_url="https://www.threads.net/@user/post/C8abc123",
        correlation_id="cid",
        request_id_override=99,
        mode="pure",
        user_id=user_id,
    )


def _crawl_result() -> Any:
    return SimpleNamespace(
        status="ok",
        content_markdown="Fallback scraper text",
        content_html=None,
        metadata_json={"title": "Threads post", "image": "https://cdn.example/photo.jpg"},
    )


def _make_extractor(
    *,
    threads_api_result: ThreadsApiExtractionResult | None,
) -> tuple[MetaPlatformExtractor, Any, Any]:
    scraper = SimpleNamespace(scrape_markdown=AsyncMock(return_value=_crawl_result()))
    threads_api = _FakeThreadsApiExtractor(threads_api_result) if threads_api_result else None
    extractor = MetaPlatformExtractor(
        cfg=SimpleNamespace(runtime=SimpleNamespace(aggregation_non_youtube_video_enabled=True)),
        scraper=scraper,
        firecrawl_sem=lambda: _DummySemCtx(),
        lifecycle=_FakeLifecycle(),
        threads_api_extractor=threads_api,
    )
    return extractor, threads_api, scraper


@pytest.mark.asyncio
async def test_threads_api_no_connection_falls_back_to_scraper_with_auth_metadata() -> None:
    extractor, threads_api, scraper = _make_extractor(
        threads_api_result=ThreadsApiExtractionResult(
            ok=False,
            metadata={
                "api_status": "no_connection",
                "provider_resource_id": "C8abc123",
                "auth_strategy": {
                    "authenticated_supported": True,
                    "selected_tier": "meta_scraper_fallback",
                },
            },
        )
    )

    result = await extractor.extract(_make_request())

    assert result.content_text == "Fallback scraper text"
    assert result.content_source == "markdown"
    assert result.metadata["api_status"] == "no_connection"
    assert result.metadata["provider_resource_id"] == "C8abc123"
    assert result.metadata["auth_strategy"]["authenticated_supported"] is True
    assert result.metadata["auth_strategy"]["selected_tier"] == "meta_scraper_fallback"
    threads_api.extract.assert_awaited_once()
    scraper.scrape_markdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_threads_api_rate_limit_metadata_survives_scraper_fallback() -> None:
    extractor, _threads_api, _scraper = _make_extractor(
        threads_api_result=ThreadsApiExtractionResult(
            ok=False,
            metadata={
                "api_status": "429",
                "provider_resource_id": "C8abc123",
                "rate_limit": {"reset": "1779519999"},
                "auth_strategy": {
                    "authenticated_supported": True,
                    "selected_tier": "meta_scraper_fallback",
                },
            },
        )
    )

    result = await extractor.extract(_make_request())

    assert result.metadata["api_status"] == "429"
    assert result.metadata["rate_limit"]["reset"] == "1779519999"
    assert result.metadata["auth_strategy"]["selected_tier"] == "meta_scraper_fallback"


@pytest.mark.asyncio
async def test_threads_api_success_skips_scraper() -> None:
    extractor, threads_api, scraper = _make_extractor(
        threads_api_result=ThreadsApiExtractionResult(
            ok=True,
            content_text="API text",
            content_source="threads_api",
            detected_lang="en",
            images=["https://cdn.threads.net/photo.jpg"],
            metadata={
                "api_status": "ok",
                "auth_strategy": {
                    "authenticated_supported": True,
                    "selected_tier": "threads_api",
                },
            },
        )
    )

    result = await extractor.extract(_make_request())

    assert result.content_text == "API text"
    assert result.content_source == "threads_api"
    assert result.metadata["auth_strategy"]["selected_tier"] == "threads_api"
    threads_api.extract.assert_awaited_once()
    scraper.scrape_markdown.assert_not_awaited()
