from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.content.content_extractor import ContentExtractionResult, ContentExtractor
from app.adapters.content.platform_extraction.models import PlatformExtractionResult
from app.application.dto.aggregation import NormalizedSourceDocument

if TYPE_CHECKING:
    from app.adapters.external.firecrawl.client import FirecrawlClient
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.config import AppConfig
    from app.db.session import DatabaseSessionManager  # type: ignore[attr-defined]


@asynccontextmanager
async def _dummy_sem():
    yield


def _dummy_cfg(
    *,
    aggregation_meta_extractors_enabled: bool = True,
    aggregation_article_media_enabled: bool = True,
) -> AppConfig:
    return cast(
        "AppConfig",
        SimpleNamespace(
            runtime=SimpleNamespace(
                enable_textacy=False,
                request_timeout_sec=5,
                aggregation_meta_extractors_enabled=aggregation_meta_extractors_enabled,
                aggregation_article_media_enabled=aggregation_article_media_enabled,
            ),
            scraper=SimpleNamespace(profile="balanced"),
            redis=SimpleNamespace(
                enabled=False,
                cache_enabled=False,
                prefix="test",
                required=False,
                cache_timeout_sec=0.1,
                firecrawl_ttl_seconds=0,
            ),
            twitter=SimpleNamespace(
                enabled=True,
                prefer_firecrawl=True,
                playwright_enabled=False,
                force_tier="auto",
                scraper_profile="inherit",
                max_concurrent_browsers=2,
                headless=True,
                page_timeout_ms=15000,
                cookies_path="/tmp/nonexistent-twitter-cookies.txt",
                article_redirect_resolution_enabled=True,
                article_resolution_timeout_sec=5.0,
            ),
            youtube=SimpleNamespace(enabled=True),
        ),
    )


def _make_extractor() -> ContentExtractor:
    firecrawl_scrape_mock = AsyncMock(
        return_value=SimpleNamespace(
            status="ok",
            content_markdown="# Title\n\n"
            + ("Substantial article body with enough useful content. " * 20),
            content_html=None,
            error_text=None,
            http_status=200,
            latency_ms=1,
            endpoint="scraper",
            metadata_json=None,
            response_success=True,
            source_url="https://example.com",
            correlation_id="cid",
            options_json=None,
        )
    )
    firecrawl = cast("FirecrawlClient", SimpleNamespace(scrape_markdown=firecrawl_scrape_mock))
    return ContentExtractor(
        cfg=_dummy_cfg(),
        db=cast("DatabaseSessionManager", SimpleNamespace()),
        firecrawl=firecrawl,  # type: ignore[arg-type]
        response_formatter=cast(
            "ResponseFormatter", SimpleNamespace(send_url_accepted_notification=AsyncMock())
        ),
        audit_func=lambda *args, **kwargs: None,
        sem=_dummy_sem,
    )


def _make_extractor_with_cfg(
    *,
    aggregation_meta_extractors_enabled: bool = True,
    aggregation_article_media_enabled: bool = True,
) -> ContentExtractor:
    firecrawl_scrape_mock = AsyncMock(
        return_value=SimpleNamespace(
            status="ok",
            content_markdown="# Title\n\n"
            + ("Substantial article body with enough useful content. " * 20),
            content_html=None,
            error_text=None,
            http_status=200,
            latency_ms=1,
            endpoint="scraper",
            metadata_json=None,
            response_success=True,
            source_url="https://example.com",
            correlation_id="cid",
            options_json=None,
        )
    )
    firecrawl = cast("FirecrawlClient", SimpleNamespace(scrape_markdown=firecrawl_scrape_mock))
    return ContentExtractor(
        cfg=_dummy_cfg(
            aggregation_meta_extractors_enabled=aggregation_meta_extractors_enabled,
            aggregation_article_media_enabled=aggregation_article_media_enabled,
        ),
        db=cast("DatabaseSessionManager", SimpleNamespace()),
        firecrawl=firecrawl,  # type: ignore[arg-type]
        response_formatter=cast(
            "ResponseFormatter", SimpleNamespace(send_url_accepted_notification=AsyncMock())
        ),
        audit_func=lambda *args, **kwargs: None,
        sem=_dummy_sem,
    )


@pytest.mark.asyncio
async def test_extract_content_pure_routes_youtube_urls_through_platform_router() -> None:
    extractor = _make_extractor()
    router = MagicMock()
    router.extract = AsyncMock(
        return_value=PlatformExtractionResult(
            platform="youtube",
            request_id=42,
            content_text="transcript text",
            content_source="youtube-transcript-api",
            detected_lang="en",
            title="Video",
            metadata={"source": "youtube"},
        )
    )
    extractor._platform_router = router

    content_text, content_source, metadata = await extractor.extract_content_pure(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        correlation_id="cid",
        request_id=42,
    )

    assert content_text == "transcript text"
    assert content_source == "youtube-transcript-api"
    assert metadata["source"] == "youtube"
    assert metadata["request_id"] == 42
    router.extract.assert_awaited_once()


@pytest.mark.asyncio
async def test_extract_content_pure_routes_twitter_urls_through_platform_router() -> None:
    extractor = _make_extractor()
    router = MagicMock()
    router.extract = AsyncMock(
        return_value=PlatformExtractionResult(
            platform="twitter",
            request_id=None,
            content_text="tweet text",
            content_source="twitter_graphql",
            detected_lang="en",
            title=None,
            metadata={"source": "twitter"},
        )
    )
    extractor._platform_router = router

    content_text, content_source, metadata = await extractor.extract_content_pure(
        "https://x.com/user/status/123?s=20&t=abc",
        correlation_id="cid",
    )

    assert content_text == "tweet text"
    assert content_source == "twitter_graphql"
    assert metadata["source"] == "twitter"
    router.extract.assert_awaited_once()


@pytest.mark.asyncio
async def test_extract_content_pure_passes_request_id_to_generic_scraper() -> None:
    extractor = _make_extractor()

    content_text, content_source, metadata = await extractor.extract_content_pure(
        "https://example.com/article",
        correlation_id="cid-req",
        request_id=777,
    )

    assert content_text
    assert content_source == "markdown"
    assert metadata["request_id"] == 777
    extractor.scraper.scrape_markdown.assert_awaited_once_with(  # type: ignore[attr-defined]
        "https://example.com/article",
        request_id=777,
    )


@pytest.mark.asyncio
async def test_extract_content_pure_passes_normalized_url_to_generic_scraper() -> None:
    extractor = _make_extractor()

    await extractor.extract_content_pure(
        "HTTPS://Example.COM/article?utm_source=newsletter&a=1",
        correlation_id="cid-normalized",
    )

    extractor.scraper.scrape_markdown.assert_awaited_once_with(  # type: ignore[attr-defined]
        "https://example.com/article?a=1",
        request_id=None,
    )


@pytest.mark.asyncio
async def test_extract_content_pure_routes_meta_urls_through_platform_router() -> None:
    extractor = cast("Any", _make_extractor())
    meta_extractor = MagicMock()
    meta_extractor.supports.return_value = True
    meta_extractor.extract = AsyncMock(
        return_value=PlatformExtractionResult(
            platform="meta",
            request_id=77,
            content_text="threads body",
            content_source="markdown",
            detected_lang="en",
            title="Threads",
            metadata={"source": "meta", "platform_surface": "threads_post"},
        )
    )
    extractor._build_meta_platform_extractor = MagicMock(return_value=meta_extractor)

    content_text, content_source, metadata = await extractor.extract_content_pure(
        "https://www.threads.net/@user/post/C8abc123",
        correlation_id="cid",
        request_id=77,
    )

    assert content_text == "threads body"
    assert content_source == "markdown"
    assert metadata["source"] == "meta"
    assert metadata["platform_surface"] == "threads_post"
    extractor._build_meta_platform_extractor.assert_called_once()


@pytest.mark.asyncio
async def test_extract_content_pure_skips_meta_router_when_feature_flag_disabled() -> None:
    extractor = cast("Any", _make_extractor_with_cfg(aggregation_meta_extractors_enabled=False))
    extractor._build_meta_platform_extractor = MagicMock()
    extractor.firecrawl.scrape_markdown = AsyncMock(
        return_value=SimpleNamespace(
            status="ok",
            content_markdown=(
                "# Threads\n\n"
                "Generic fallback body with enough narrative detail to look like a real article. "
                "It explains how a creator posted a product update, why the audience reacted, "
                "and what changed after the first announcement.\n\n"
                "A second paragraph adds context about the feature rollout, the audience feedback, "
                "and the follow-up clarifications so the generic extractor keeps the page as "
                "substantive content instead of rejecting it as navigation chrome."
            ),
            content_html=None,
            error_text=None,
            http_status=200,
            latency_ms=1,
            endpoint="scraper",
            metadata_json={"title": "Threads fallback"},
            response_success=True,
            source_url="https://www.threads.net/@user/post/C8abc123",
            correlation_id="cid",
            options_json=None,
        )
    )

    content_text, content_source, metadata = await extractor.extract_content_pure(
        "https://www.threads.net/@user/post/C8abc123",
        correlation_id="cid",
        request_id=77,
    )

    assert "Generic fallback body" in content_text
    assert content_source == "markdown"
    extractor._build_meta_platform_extractor.assert_not_called()
    assert "normalized_source_document" in metadata


@pytest.mark.asyncio
async def test_extract_content_pure_disables_article_media_when_flag_off() -> None:
    extractor = _make_extractor_with_cfg(aggregation_article_media_enabled=False)
    extractor.firecrawl.scrape_markdown = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            status="ok",
            content_markdown=(
                "# Title\n\n"
                "This article has enough substantive body text to bypass the low-value guard. "
                "It explains the rollout, the audience reaction, and the follow-up context "
                "in a way that looks like a real article instead of a stub."
            ),
            content_html=None,
            error_text=None,
            http_status=200,
            latency_ms=1,
            endpoint="scraper",
            metadata_json={
                "title": "Title",
                "image_urls": ["https://cdn.example.com/hero.jpg"],
            },
            response_success=True,
            source_url="https://example.com/article",
            correlation_id="cid",
            options_json=None,
        )
    )

    _content_text, _content_source, metadata = await extractor.extract_content_pure(
        "https://example.com/article",
        correlation_id="cid",
    )

    assert metadata["media_selection"]["strategy"] == "disabled_by_runtime_flag"
    assert metadata["normalized_source_document"]["media"] == []


@pytest.mark.asyncio
async def test_extract_and_process_content_routes_platform_urls_before_generic_scrape() -> None:
    extractor = _make_extractor()
    router = MagicMock()
    router.extract = AsyncMock(
        return_value=PlatformExtractionResult(
            platform="twitter",
            request_id=9,
            content_text="tweet text",
            content_source="twitter_graphql",
            detected_lang="en",
            title="Title",
            images=[],
            metadata={"source": "twitter"},
        )
    )
    extractor._platform_router = router

    result = await extractor.extract_and_process_content(
        message=MagicMock(),
        url_text="https://x.com/user/status/123",
        correlation_id="cid",
        interaction_id=None,
        silent=True,
    )

    assert result == ContentExtractionResult(
        request_id=9,
        content_text="tweet text",
        content_source="twitter_graphql",
        detected_lang="en",
        title="Title",
        images=[],
    )
    extractor.firecrawl.scrape_markdown.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_extract_content_pure_builds_multimodal_article_document_from_firecrawl_images() -> (
    None
):
    extractor: Any = _make_extractor()
    router = MagicMock()
    router.extract = AsyncMock(return_value=None)
    extractor._platform_router = router
    extractor.firecrawl.scrape_markdown = AsyncMock(
        return_value=SimpleNamespace(
            status="ok",
            content_markdown=(
                "# Title\n\n"
                "This article explains the quarterly business results in detail, including "
                "revenue changes, segment performance, and executive commentary. "
                "It also breaks down the charts, highlights regional variance, and "
                "summarizes management guidance for the next quarter."
            ),
            content_html=None,
            error_text=None,
            http_status=200,
            latency_ms=1,
            endpoint="scraper",
            metadata_json={
                "title": "Example article",
                "images": [
                    {
                        "url": "https://cdn.example.com/chart.png",
                        "alt": "Quarterly revenue chart",
                        "width": 1280,
                        "height": 720,
                    },
                    {
                        "url": "https://cdn.example.com/logo.svg",
                        "alt": "Site logo",
                    },
                ],
                "og:image": "https://cdn.example.com/chart.png",
            },
            response_success=True,
            source_url="https://example.com/article",
            correlation_id="cid",
            options_json=None,
        )
    )

    content_text, content_source, metadata = await extractor.extract_content_pure(
        "https://example.com/article",
        correlation_id="cid",
        request_id=42,
    )

    assert content_text.startswith("# Title")
    assert content_source == "markdown"
    normalized_document = NormalizedSourceDocument.model_validate(
        metadata["normalized_source_document"]
    )
    assert normalized_document.media[0].url == "https://cdn.example.com/chart.png"
    assert normalized_document.media[0].alt_text == "Quarterly revenue chart"
    assert len(normalized_document.media) == 1
    assert metadata["media_selection"]["selected_count"] == 1
    assert metadata["media_selection"]["rejected_reasons"]["blocked_extension"] == 1


@pytest.mark.asyncio
async def test_generic_urls_fall_back_to_existing_scraper_chain_when_router_misses() -> None:
    extractor: Any = _make_extractor()
    router = MagicMock()
    router.extract = AsyncMock(return_value=None)
    extractor._platform_router = router
    extractor._handle_request_dedupe_or_create = AsyncMock(return_value=55)
    extractor._extract_or_reuse_content_with_title = AsyncMock(
        return_value=("body", "markdown", "Title", [])
    )

    result = await extractor.extract_and_process_content(
        message=MagicMock(),
        url_text="https://example.com/article",
        correlation_id="cid",
        interaction_id=None,
        silent=True,
    )

    assert result == ContentExtractionResult(
        request_id=55,
        content_text="body",
        content_source="markdown",
        detected_lang="en",
        title="Title",
        images=[],
    )
    extractor._handle_request_dedupe_or_create.assert_awaited_once()
