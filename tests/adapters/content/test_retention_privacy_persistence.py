"""Tests for retention-aware raw payload persistence guards."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.content.content_extractor_requests import ContentExtractorRequestsMixin
from app.adapters.content.llm_response_workflow_storage import LLMWorkflowStorageMixin


def _cfg(*, no_retention: bool = True, llm_policy: str = "full") -> SimpleNamespace:
    return SimpleNamespace(
        openrouter=SimpleNamespace(model="fallback-model"),
        retention=SimpleNamespace(
            persist_raw_extracted_content=not no_retention,
            persist_llm_prompt_response_payloads=not no_retention and llm_policy == "full",
        ),
    )


def test_llm_payload_builder_strips_prompts_and_responses_in_no_retention_mode() -> None:
    storage = LLMWorkflowStorageMixin()
    storage.cfg = _cfg(no_retention=True)
    llm = SimpleNamespace(
        model="model-a",
        endpoint="/chat",
        request_headers={"Authorization": "Bearer secret"},
        request_messages=[{"role": "user", "content": "raw prompt"}],
        response_text="raw response",
        response_json={"answer": "raw"},
        tokens_prompt=10,
        tokens_completion=20,
        cost_usd=0.01,
        latency_ms=123,
        status="ok",
        error_text=None,
        error_context=None,
    )

    payload = storage._build_llm_call_payload(llm, req_id=42)

    assert payload["request_headers_json"] == {}
    assert payload["request_messages_json"] == []
    assert payload["response_text"] is None
    assert payload["response_json"] == {}
    assert payload["tokens_prompt"] == 10
    assert payload["tokens_completion"] == 20
    assert payload["cost_usd"] == 0.01


@pytest.mark.asyncio
async def test_crawl_persistence_strips_raw_content_in_no_retention_mode() -> None:
    mixin = ContentExtractorRequestsMixin()
    mixin.cfg = _cfg(no_retention=True)
    crawl_repo = AsyncMock()
    mixin.message_persistence = SimpleNamespace(crawl_repo=crawl_repo)
    crawl = SimpleNamespace(
        response_success=True,
        content_markdown="raw markdown",
        content_html="<p>raw html</p>",
        error_text=None,
        metadata_json={"title": "Saved title", "html": "<secret>"},
        source_url="https://example.test/post",
        http_status=200,
        status="ok",
        endpoint="firecrawl",
        latency_ms=50,
        correlation_id="cid",
        options_json={"formats": ["markdown"]},
    )

    await mixin._persist_crawl_result(10, crawl, "cid")

    kwargs = crawl_repo.async_insert_crawl_result.await_args.kwargs
    assert kwargs["markdown"] is None
    assert kwargs["html"] is None
    assert kwargs["metadata_json"] == {"title": "Saved title", "raw_payload_persisted": False}
    assert kwargs["source_url"] == "https://example.test/post"
