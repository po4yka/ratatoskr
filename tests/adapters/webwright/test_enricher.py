"""Tests for WebwrightEnricher."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.adapters.webwright.client import WebwrightTaskResult
from app.adapters.webwright.enricher import EnrichmentResult, WebwrightEnricher


def _fake_client(*, status: str = "ok", body: str = "x" * 500) -> Any:
    client = AsyncMock()
    client.run_task = AsyncMock(
        return_value=WebwrightTaskResult(
            status=status,
            final_answer=(
                '{"title":"T","body_markdown":"' + body + '","metadata":{}}'
                if status == "ok"
                else None
            ),
            screenshots=(),
            trajectory_path="/tmp/x",
            steps_used=4,
            llm_cost_usd=0.01,
            error_text=None if status == "ok" else "boom",
            latency_ms=500,
            correlation_id="c1",
        )
    )
    return client


@pytest.mark.asyncio(loop_scope="function")
async def test_skips_when_host_not_allowlisted():
    enricher = WebwrightEnricher(
        client=_fake_client(),
        host_allowlist=("only-this-host.com",),
    )
    out = await enricher.maybe_enrich_url(
        url="https://example.com/x",
        current_content="",
        correlation_id="c1",
    )
    assert out is None


@pytest.mark.asyncio(loop_scope="function")
async def test_skips_when_url_empty():
    enricher = WebwrightEnricher(
        client=_fake_client(),
        host_allowlist=("example.com",),
    )
    out = await enricher.maybe_enrich_url(url="", current_content=None, correlation_id=None)
    assert out is None


@pytest.mark.asyncio(loop_scope="function")
async def test_skips_when_existing_content_is_sufficient():
    enricher = WebwrightEnricher(
        client=_fake_client(),
        host_allowlist=("example.com",),
        min_content_length=100,
    )
    plenty = "y" * 500
    out = await enricher.maybe_enrich_url(
        url="https://example.com/x",
        current_content=plenty,
        correlation_id="c1",
    )
    assert out is None


@pytest.mark.asyncio(loop_scope="function")
async def test_enrichment_returns_parsed_result():
    client = _fake_client(body="A" * 800)
    enricher = WebwrightEnricher(
        client=client,
        host_allowlist=("example.com",),
        min_content_length=100,
    )
    out = await enricher.maybe_enrich_url(
        url="https://example.com/article",
        current_content="thin",
        correlation_id="c1",
    )
    assert isinstance(out, EnrichmentResult)
    assert out.title == "T"
    assert out.body_markdown.startswith("AAAA")
    assert out.steps_used == 4
    assert out.llm_cost_usd == 0.01
    assert out.trajectory_path == "/tmp/x"
    # Allowed domains should match the URL's host.
    _, kwargs = client.run_task.call_args
    assert kwargs["allowed_domains"] == ("example.com",)


@pytest.mark.asyncio(loop_scope="function")
async def test_returns_none_when_sidecar_fails():
    enricher = WebwrightEnricher(
        client=_fake_client(status="error"),
        host_allowlist=("example.com",),
        min_content_length=100,
    )
    out = await enricher.maybe_enrich_url(
        url="https://example.com/x",
        current_content=None,
        correlation_id="c1",
    )
    assert out is None


@pytest.mark.asyncio(loop_scope="function")
async def test_returns_none_when_enriched_body_still_thin():
    client = _fake_client(body="tiny")  # < min_content_length
    enricher = WebwrightEnricher(
        client=client,
        host_allowlist=("example.com",),
        min_content_length=400,
    )
    out = await enricher.maybe_enrich_url(
        url="https://example.com/x",
        current_content="",
        correlation_id="c1",
    )
    assert out is None


@pytest.mark.asyncio(loop_scope="function")
async def test_wildcard_allowlist_accepts_any_host():
    enricher = WebwrightEnricher(
        client=_fake_client(body="z" * 800),
        host_allowlist=("*",),
        min_content_length=100,
    )
    out = await enricher.maybe_enrich_url(
        url="https://novel-host.io/article",
        current_content="",
        correlation_id="c1",
    )
    assert isinstance(out, EnrichmentResult)
