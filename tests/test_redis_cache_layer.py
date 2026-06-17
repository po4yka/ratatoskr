import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.content.content_extractor import ContentExtractor, FirecrawlResult
from app.infrastructure.cache.redis_cache import RedisCache


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


def _dummy_cfg() -> SimpleNamespace:
    redis_cfg = SimpleNamespace(
        enabled=True,
        cache_enabled=True,
        required=False,
        prefix="test",
        cache_timeout_sec=0.1,
        firecrawl_ttl_seconds=60,
        llm_ttl_seconds=60,
    )
    runtime_cfg = SimpleNamespace(
        enable_textacy=False,
        request_timeout_sec=5,
        summary_prompt_version="v1",
    )
    openrouter_cfg = SimpleNamespace(
        model="test-model",
        structured_output_mode="json_schema",
        max_tokens=None,
        temperature=0.1,
        top_p=1.0,
        fallback_models=(),
        long_context_model=None,
        enable_structured_outputs=True,
        require_parameters=True,
        auto_fallback_structured=True,
        max_response_size_mb=10,
        summary_temperature_relaxed=None,
        summary_top_p_relaxed=None,
        summary_temperature_json_fallback=None,
        summary_top_p_json_fallback=None,
    )
    firecrawl_cfg = SimpleNamespace()
    web_search_cfg = SimpleNamespace(enabled=False)
    model_routing_cfg = SimpleNamespace(enabled=False, fallback_models=())
    attachment_cfg = SimpleNamespace(article_vision_enabled=False, vision_model=None)
    return SimpleNamespace(
        redis=redis_cfg,
        runtime=runtime_cfg,
        openrouter=openrouter_cfg,
        firecrawl=firecrawl_cfg,
        web_search=web_search_cfg,
        model_routing=model_routing_cfg,
        attachment=attachment_cfg,
    )


@pytest.mark.asyncio
async def test_redis_cache_set_get_roundtrip():
    cfg = _dummy_cfg()
    cache = RedisCache(cfg)
    fake_client = _FakeRedis()
    cache._client = fake_client

    stored = {"hello": "world"}
    ok = await cache.set_json(value=stored, ttl_seconds=10, parts=("fc", "v1", "abc"))
    assert ok

    result = await cache.get_json("fc", "v1", "abc")
    assert result == stored


@pytest.mark.asyncio
async def test_content_extractor_uses_cached_crawl(monkeypatch):
    cfg = _dummy_cfg()
    db = SimpleNamespace()

    async def _scrape_markdown(*args, **kwargs):
        raise AssertionError("scrape_markdown should not be called when cache hits")

    firecrawl = SimpleNamespace(scrape_markdown=_scrape_markdown)

    response_formatter = SimpleNamespace(
        send_content_reuse_notification=AsyncMock(),
        send_firecrawl_start_notification=AsyncMock(),
        send_html_fallback_notification=AsyncMock(),
    )

    extractor = ContentExtractor(
        cfg=cfg,
        db=db,
        firecrawl=firecrawl,
        response_formatter=response_formatter,
        audit_func=lambda *args, **kwargs: None,
        sem=lambda: asyncio.Semaphore(1),
    )

    cached = FirecrawlResult(
        status="ok",
        http_status=200,
        content_markdown="cached text",
        content_html=None,
        structured_json=None,
        metadata_json=None,
        links_json=None,
        response_success=True,
        response_error_code=None,
        response_error_message=None,
        response_details=None,
        latency_ms=10,
        error_text=None,
        source_url="http://example.com",
        endpoint="cache",
        options_json=None,
        correlation_id=None,
    )

    extractor._get_cached_crawl = AsyncMock(return_value=cached)
    extractor._write_firecrawl_cache = AsyncMock()

    extractor._schedule_crawl_persistence = lambda *args, **kwargs: None

    async def _process_successful_crawl_with_title(*args, **kwargs):
        return ("cached text", "markdown", "Cached Title", [])

    extractor._process_successful_crawl_with_title = _process_successful_crawl_with_title

    result = await extractor._perform_new_crawl_with_title(
        message=None,
        req_id=1,
        url_text="http://example.com",
        dedupe_hash="hash",
        correlation_id=None,
        interaction_id=None,
        silent=True,
    )

    assert result == ("cached text", "markdown", "Cached Title", [])
    response_formatter.send_firecrawl_start_notification.assert_not_awaited()
    response_formatter.send_content_reuse_notification.assert_awaited_once()
