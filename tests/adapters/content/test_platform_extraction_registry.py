from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.content.content_extractor import ContentExtractor
from app.adapters.content.platform_extraction import (
    PlatformExtractionRequest,
    PlatformExtractionResult,
    PlatformExtractorBuildContext,
    PlatformExtractorDescriptor,
    build_platform_extraction_router,
)
from app.config import AppConfig
from app.di.platform_extractors import build_platform_extractor_descriptors


@asynccontextmanager
async def _dummy_sem():
    yield


def _cfg() -> AppConfig:
    return cast(
        "AppConfig",
        SimpleNamespace(
            runtime=SimpleNamespace(
                aggregation_meta_extractors_enabled=True,
                aggregation_article_media_enabled=True,
            ),
            redis=SimpleNamespace(
                enabled=False,
                cache_enabled=False,
                prefix="test",
                required=False,
                cache_timeout_sec=0.1,
                firecrawl_ttl_seconds=0,
            ),
            twitter=SimpleNamespace(enabled=True),
            scraper=SimpleNamespace(profile="balanced"),
            youtube=SimpleNamespace(enabled=True),
        ),
    )


def _context(cfg: AppConfig | None = None) -> PlatformExtractorBuildContext:
    message_persistence = MagicMock(name="message_persistence")
    return PlatformExtractorBuildContext(
        cfg=cfg or _cfg(),
        db=MagicMock(name="db"),
        scraper=MagicMock(name="scraper"),
        response_formatter=MagicMock(name="response_formatter"),
        audit_func=lambda *args, **kwargs: None,
        sem=lambda: MagicMock(),
        message_persistence=message_persistence,
        lifecycle=MagicMock(name="lifecycle"),
        quality_llm_client=MagicMock(name="quality_llm_client"),
        schedule_crawl_persistence=lambda *args, **kwargs: None,
    )


class _FakeExtractor:
    def __init__(self, platform: str) -> None:
        self.platform = platform
        self.extract = AsyncMock(
            return_value=PlatformExtractionResult(
                platform=platform,
                request_id=123,
                content_text=f"{platform} content",
                content_source=f"{platform}_api",
                detected_lang="en",
                metadata={"source": platform},
            )
        )

    def supports(self, _normalized_url: str) -> bool:
        return True


def _make_request(url: str) -> PlatformExtractionRequest:
    return PlatformExtractionRequest(
        message=None,
        url_text=url,
        normalized_url=url,
        correlation_id="cid",
        silent=True,
        mode="pure",
    )


def _fake_descriptors_from_builtins(
    cfg: AppConfig,
) -> tuple[tuple[PlatformExtractorDescriptor, ...], dict[str, _FakeExtractor]]:
    extractors = {
        descriptor.name: _FakeExtractor(descriptor.name)
        for descriptor in build_platform_extractor_descriptors(cfg)
    }
    descriptors = tuple(
        PlatformExtractorDescriptor(
            name=descriptor.name,
            predicate=descriptor.predicate,
            factory=lambda _context, name=descriptor.name: extractors[name],
        )
        for descriptor in build_platform_extractor_descriptors(cfg)
    )
    return descriptors, extractors


def _make_content_extractor(
    descriptor: PlatformExtractorDescriptor,
) -> tuple[ContentExtractor, AsyncMock]:
    scrape_markdown = AsyncMock()
    scraper = SimpleNamespace(scrape_markdown=scrape_markdown)
    cfg = _cfg()
    router = build_platform_extraction_router((descriptor,), _context(cfg))
    extractor = ContentExtractor(
        cfg=cfg,
        db=cast("Any", SimpleNamespace()),
        firecrawl=cast("Any", scraper),
        response_formatter=cast(
            "Any",
            SimpleNamespace(send_url_accepted_notification=AsyncMock()),
        ),
        audit_func=lambda *args, **kwargs: None,
        sem=_dummy_sem,
        platform_router=router,
    )
    return extractor, scrape_markdown


def test_builtin_platform_descriptors_preserve_route_order() -> None:
    descriptors = build_platform_extractor_descriptors(_cfg())

    assert [descriptor.name for descriptor in descriptors] == [
        "github",
        "academic",
        "youtube",
        "twitter",
        "meta",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "expected_platform"),
    [
        ("https://github.com/openai/openai-python", "github"),
        ("https://arxiv.org/abs/2301.00001", "academic"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("https://x.com/user/status/123", "twitter"),
        ("https://twitter.com/user/status/123", "twitter"),
        ("https://www.threads.net/@user/post/C8abc123", "meta"),
        ("https://www.instagram.com/p/C8abc123/", "meta"),
    ],
)
async def test_builtin_descriptors_route_platform_urls_to_explicit_extractors(
    url: str,
    expected_platform: str,
) -> None:
    cfg = _cfg()
    descriptors, extractors = _fake_descriptors_from_builtins(cfg)
    router = build_platform_extraction_router(descriptors, _context(cfg))

    result = await router.extract(_make_request(url))

    assert result is not None
    assert result.platform == expected_platform
    extractors[expected_platform].extract.assert_awaited_once()
    for name, extractor in extractors.items():
        if name != expected_platform:
            extractor.extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_github_descriptor_runs_before_content_extractor_generic_scraper() -> None:
    from app.adapters.github.url_patterns import is_github_repo_url

    github_extractor = _FakeExtractor("github")
    descriptor = PlatformExtractorDescriptor(
        name="github",
        predicate=is_github_repo_url,
        factory=lambda _context: github_extractor,
    )
    extractor, scrape_markdown = _make_content_extractor(descriptor)

    content_text, content_source, metadata = await extractor.extract_content_pure(
        "https://github.com/openai/openai-python",
        correlation_id="cid",
        request_id=123,
    )

    assert content_text == "github content"
    assert content_source == "github_api"
    assert metadata["source"] == "github"
    github_extractor.extract.assert_awaited_once()
    scrape_markdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_fake_descriptor_adds_extractor_without_content_extractor_changes() -> None:
    fake_extractor = _FakeExtractor("new-platform")
    descriptor = PlatformExtractorDescriptor(
        name="new-platform",
        predicate=lambda normalized_url: normalized_url == "https://new.example/item",
        factory=lambda _context: fake_extractor,
    )
    extractor, scrape_markdown = _make_content_extractor(descriptor)

    content_text, content_source, metadata = await extractor.extract_content_pure(
        "https://new.example/item",
        correlation_id="cid",
        request_id=123,
    )

    assert content_text == "new-platform content"
    assert content_source == "new-platform_api"
    assert metadata["source"] == "new-platform"
    fake_extractor.extract.assert_awaited_once()
    scrape_markdown.assert_not_awaited()
