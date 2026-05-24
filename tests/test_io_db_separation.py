from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.adapters.content.content_extractor import ContentExtractor
from app.adapters.content.llm_response_workflow import (
    AttemptContext,
    LLMInteractionConfig,
    LLMResponseWorkflow,
    LLMSummaryPersistenceSettings,
)
from app.adapters.external.firecrawl.client import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.lang import LANG_EN


class _DummySemaphore:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _NoopFormatter:
    async def send_firecrawl_start_notification(self, *args, **kwargs) -> None:
        return None

    async def send_firecrawl_success_notification(self, *args, **kwargs) -> None:
        return None

    async def send_html_fallback_notification(self, *args, **kwargs) -> None:
        return None


def _workflow_repo_kwargs() -> dict[str, Any]:
    return {
        "summary_repo": MagicMock(),
        "request_repo": MagicMock(),
        "llm_repo": MagicMock(),
        "user_repo": MagicMock(),
    }


@pytest.mark.asyncio
async def test_firecrawl_persistence_runs_in_background() -> None:
    """Firecrawl extraction should not block on DB persistence."""
    persist_started = asyncio.Event()
    persist_release = asyncio.Event()

    async def slow_persist(
        self: ContentExtractor, req_id: int, crawl: FirecrawlResult, cid: str | None
    ) -> None:
        persist_started.set()
        await persist_release.wait()

    async def fake_scrape(url: str, request_id: int | None = None) -> FirecrawlResult:
        return FirecrawlResult(
            status=CallStatus.OK,
            http_status=200,
            content_markdown=" ".join(["hello world"] * 80),
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
            source_url=url,
            endpoint="/v2/scrape",
            options_json=None,
            correlation_id=None,
        )

    extractor: Any = ContentExtractor.__new__(ContentExtractor)
    extractor.cfg = SimpleNamespace(runtime=SimpleNamespace(enable_textacy=False))
    extractor.db = SimpleNamespace()
    extractor.firecrawl = SimpleNamespace(scrape_markdown=fake_scrape)
    extractor.response_formatter = _NoopFormatter()
    extractor._audit = lambda *args, **kwargs: None
    extractor._sem = lambda: _DummySemaphore()
    extractor._persist_crawl_result = MethodType(slow_persist, extractor)
    extractor._cache = SimpleNamespace(enabled=False)

    content_text, content_source, _title, _images = await asyncio.wait_for(
        extractor._perform_new_crawl_with_title(
            message=SimpleNamespace(),
            req_id=1,
            url_text="https://example.com",
            dedupe_hash="hash",
            correlation_id="cid-firecrawl",
            interaction_id=None,
            silent=False,
        ),
        timeout=0.5,
    )

    await asyncio.wait_for(persist_started.wait(), timeout=0.2)
    assert content_source == "markdown"
    assert "hello world" in content_text
    assert persist_started.is_set()
    assert not persist_release.is_set()

    persist_release.set()
    await asyncio.wait_for(persist_started.wait(), timeout=0.2)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_summary_persistence_deferred_from_llm_flow() -> None:
    """LLM workflow should return before deferred summary persistence finishes."""
    persist_started = asyncio.Event()
    persist_release = asyncio.Event()

    class _SlowSummaryRepo:
        async def async_finalize_request_summary(self, **kwargs) -> int:
            persist_started.set()
            await persist_release.wait()
            return 42

    class _SlowRequestRepo:
        async def async_update_request_status(self, req_id: int, status: str) -> None:
            await persist_release.wait()

    # Create workflow with a mock db - the actual db won't be used since we override repos
    workflow = LLMResponseWorkflow(
        cfg=SimpleNamespace(
            openrouter=SimpleNamespace(model="test-model"),
            runtime=SimpleNamespace(
                semaphore_acquire_timeout_sec=30.0,
                llm_call_timeout_sec=180.0,
            ),
        ),
        db=SimpleNamespace(),  # type: ignore[arg-type]  # Mock db, won't be used
        llm_client=MagicMock(),
        response_formatter=None,
        audit_func=lambda *args, **kwargs: None,
        sem=lambda: _DummySemaphore(),
        **_workflow_repo_kwargs(),
    )
    # Override repositories with slow mocks
    workflow.summary_repo = _SlowSummaryRepo()
    workflow.request_repo = _SlowRequestRepo()

    summary = {"summary_250": "short", "summary_1000": "long form", "tldr": "tldr"}
    llm_stub = SimpleNamespace(status="ok", latency_ms=5, model="m")
    interaction_config = LLMInteractionConfig()
    persistence = LLMSummaryPersistenceSettings(
        lang=LANG_EN, is_read=True, defer_write=True, insights_getter=lambda _: None
    )
    ctx = AttemptContext(
        message=SimpleNamespace(),
        llm=llm_stub,
        req_id=11,
        correlation_id="cid-llm",
        interaction_config=interaction_config,
        persistence=persistence,
        ensure_summary=None,
        on_success=None,
        defer_persistence=True,
    )

    result = await workflow.finalize_success(ctx, summary)

    await asyncio.wait_for(persist_started.wait(), timeout=0.2)
    assert result["summary_250"].startswith("short")
    assert persist_started.is_set()
    assert not persist_release.is_set()

    persist_release.set()
    await asyncio.wait_for(persist_started.wait(), timeout=0.2)
    await asyncio.sleep(0)
