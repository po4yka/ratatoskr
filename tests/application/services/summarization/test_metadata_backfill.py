"""Branch coverage for ``metadata_backfill`` helpers + the public backfill flow.

Targets the uncovered raw-payload fallback, the ``_flatten_node`` recursion /
key-hint extraction, and the ``_extract_heading_title`` heuristics (audit #12),
plus an end-to-end pass through ``backfill_summary_metadata`` covering the
request-URL / domain / heading steps against fake ports.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.application.services.summarization.metadata_backfill import (
    _extract_heading_title,
    _flatten_crawl_metadata,
    _flatten_node,
    backfill_summary_metadata,
)

# --------------------------------------------------------------------------- #
# _flatten_crawl_metadata: raw_response_json fallback when metadata_json absent
# --------------------------------------------------------------------------- #


def test_flatten_uses_metadata_json_dict_directly() -> None:
    flat = _flatten_crawl_metadata({"metadata_json": {"og:title": "Hello"}})
    assert flat["og:title"] == "Hello"


def test_flatten_parses_metadata_json_string() -> None:
    flat = _flatten_crawl_metadata({"metadata_json": json.dumps({"author": "A. Writer"})})
    assert flat["author"] == "A. Writer"


def test_flatten_ignores_unparseable_metadata_json_then_falls_back() -> None:
    """Bad metadata_json JSON is swallowed; the raw_response_json fallback fills in."""
    row = {
        "metadata_json": "{not json",
        "raw_response_json": {"data": {"metadata": {"title": "From Raw"}}},
    }
    flat = _flatten_crawl_metadata(row)
    assert flat["title"] == "From Raw"


def test_flatten_raw_payload_fallback_from_json_string() -> None:
    """raw_response_json as a JSON string -> data.metadata is flattened (lines 250-259)."""
    raw = json.dumps({"data": {"metadata": {"og:url": "https://x.example/a"}}})
    flat = _flatten_crawl_metadata({"raw_response_json": raw})
    assert flat["og:url"] == "https://x.example/a"


def test_flatten_raw_payload_fallback_uses_meta_key_alias() -> None:
    """The fallback accepts ``data.meta`` when ``data.metadata`` is absent."""
    row = {"raw_response_json": {"data": {"meta": {"headline": "Meta Headline"}}}}
    flat = _flatten_crawl_metadata(row)
    assert flat["headline"] == "Meta Headline"


def test_flatten_raw_payload_unparseable_returns_empty() -> None:
    flat = _flatten_crawl_metadata({"raw_response_json": "{broken"})
    assert flat == {}


def test_flatten_returns_empty_when_no_metadata_sources() -> None:
    assert _flatten_crawl_metadata({}) == {}


# --------------------------------------------------------------------------- #
# _flatten_node: recursion + property/name key-hint extraction
# --------------------------------------------------------------------------- #


def test_flatten_node_scalar_and_none_are_noops() -> None:
    collector: dict[str, str] = {}
    _flatten_node(None, collector)
    _flatten_node("string", collector)
    _flatten_node(42, collector)
    assert collector == {}


def test_flatten_node_extracts_property_content_pair() -> None:
    """A {'property': 'og:title', 'content': '...'} node maps property -> content."""
    collector: dict[str, str] = {}
    _flatten_node({"property": "og:title", "content": "Recursed Title"}, collector)
    assert collector["og:title"] == "Recursed Title"


def test_flatten_node_recurses_into_nested_lists_and_dicts() -> None:
    """Nested list of meta-tag dicts is flattened recursively (lines 286-297)."""
    node = {
        "meta": [
            {"name": "author", "content": "Nested Author"},
            {"property": "article:published_time", "value": "2026-01-02"},
        ],
        "title": "Top Level Title",
    }
    collector: dict[str, str] = {}
    _flatten_node(node, collector)
    assert collector["author"] == "Nested Author"
    assert collector["article:published_time"] == "2026-01-02"
    # Plain scalar child is captured under its normalized key.
    assert collector["title"] == "Top Level Title"


def test_flatten_node_first_writer_wins_on_key_collision() -> None:
    """setdefault / 'key_hint not in collector' means the first value sticks."""
    collector: dict[str, str] = {"title": "First"}
    _flatten_node({"title": "Second"}, collector)
    assert collector["title"] == "First"


# --------------------------------------------------------------------------- #
# _extract_heading_title: markdown heading -> title: line -> first short line
# --------------------------------------------------------------------------- #


def test_heading_title_prefers_markdown_heading() -> None:
    assert _extract_heading_title("intro\n## Real Heading\nbody") == "Real Heading"


def test_heading_title_empty_content_returns_none() -> None:
    assert _extract_heading_title("") is None


def test_heading_title_blank_only_lines_return_none() -> None:
    assert _extract_heading_title("   \n\t\n  ") is None


def test_heading_title_extracts_title_prefix_line() -> None:
    """A ``Title: X | extra`` line yields the part before the pipe (lines 316-321)."""
    text = "[source: yt]\nTitle: My Video Title | channel stuff\nbody"
    assert _extract_heading_title(text) == "My Video Title"


def test_heading_title_skips_source_preamble_and_metadata_prefixes() -> None:
    """``[source:...]`` preamble and channel:/duration:/resolution: lines are skipped."""
    text = "[source: youtube]\nchannel: Some Channel\nduration: 10:00\nActual Heading Line"
    assert _extract_heading_title(text) == "Actual Heading Line"


def test_heading_title_skips_overlong_lines() -> None:
    """A leading line over 140 chars is rejected; a later short line wins."""
    long_line = "x" * 200
    text = f"{long_line}\nShort title"
    assert _extract_heading_title(text) == "Short title"


def test_heading_title_returns_none_when_all_candidates_rejected() -> None:
    text = "[source: web]\nchannel: c\n" + ("y" * 200)
    assert _extract_heading_title(text) is None


def test_heading_title_continues_past_empty_title_prefix() -> None:
    """A ``Title:`` line with no value after the colon is skipped, not returned."""
    text = "Title: \nReal fallback line"
    assert _extract_heading_title(text) == "Real fallback line"


# --------------------------------------------------------------------------- #
# backfill_summary_metadata: end-to-end request-URL / domain / heading steps
# --------------------------------------------------------------------------- #


class _FakeCrawlRepo:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    async def async_get_crawl_result_by_request(self, request_id: int) -> dict[str, Any] | None:
        return self._row


class _FakeRequestRepo:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    async def async_get_request_by_id(self, request_id: int) -> dict[str, Any] | None:
        return self._row


@pytest.mark.asyncio
async def test_backfill_short_circuits_when_nothing_missing() -> None:
    summary = {
        "metadata": {
            "title": "t",
            "canonical_url": "https://e.example/a",
            "domain": "e.example",
            "author": "a",
            "published_at": "2026-01-01",
            "last_updated": "2026-01-02",
        }
    }
    out = await backfill_summary_metadata(
        summary,
        request_id=1,
        content_text="# Heading",
        correlation_id="cid",
        request_repo=_FakeRequestRepo(None),
        crawl_repo=_FakeCrawlRepo(None),
    )
    assert out["metadata"]["title"] == "t"


@pytest.mark.asyncio
async def test_backfill_fills_canonical_domain_and_heading_title() -> None:
    """Steps 2-4: request URL -> canonical_url -> domain, heading -> title."""
    summary: dict[str, Any] = {"metadata": {}}
    out = await backfill_summary_metadata(
        summary,
        request_id=7,
        content_text="## Derived Heading Title\nbody text",
        correlation_id="cid-bf",
        request_repo=_FakeRequestRepo({"normalized_url": "https://news.example/post-1"}),
        crawl_repo=_FakeCrawlRepo(None),
    )
    md = out["metadata"]
    assert md["canonical_url"] == "https://news.example/post-1"
    assert md["domain"] == "news.example"
    assert md["title"] == "Derived Heading Title"


@pytest.mark.asyncio
async def test_backfill_creates_metadata_dict_when_absent() -> None:
    """A summary with no ``metadata`` key gets one created (lines 121-123)."""
    summary: dict[str, Any] = {}
    out = await backfill_summary_metadata(
        summary,
        request_id=2,
        content_text="# Title From Heading",
        correlation_id=None,
        request_repo=_FakeRequestRepo(None),
        crawl_repo=_FakeCrawlRepo(None),
    )
    assert out["metadata"]["title"] == "Title From Heading"


@pytest.mark.asyncio
async def test_backfill_applies_firecrawl_aliases_from_raw_payload() -> None:
    """Step 1 end-to-end: crawl raw_response_json fallback -> alias fill."""
    summary: dict[str, Any] = {"metadata": {}}
    crawl_row = {
        "raw_response_json": {
            "data": {"metadata": {"og:title": "Firecrawl Title", "author": "F. Author"}}
        }
    }
    out = await backfill_summary_metadata(
        summary,
        request_id=3,
        content_text="",
        correlation_id="cid-fc",
        request_repo=_FakeRequestRepo(None),
        crawl_repo=_FakeCrawlRepo(crawl_row),
    )
    md = out["metadata"]
    assert md["title"] == "Firecrawl Title"
    assert md["author"] == "F. Author"


@pytest.mark.asyncio
async def test_backfill_returns_early_when_crawl_fills_everything() -> None:
    """Step 1 filling all missing fields short-circuits before the request lookup
    (line 144). The request repo must therefore never be consulted."""

    class _ExplodingRequestRepo:
        async def async_get_request_by_id(self, request_id: int) -> Any:
            raise AssertionError("request repo must not be reached")

    # ``domain`` has no firecrawl alias, so it must be pre-populated for step 1 to
    # empty ``missing`` entirely; the crawl row then fills the other five fields and
    # the ``if not missing: return`` (line 144) fires before the request lookup.
    summary: dict[str, Any] = {"metadata": {"domain": "e.example"}}
    crawl_row = {
        "metadata_json": {
            "title": "T",
            "canonical": "https://e.example/a",
            "author": "A",
            "article:published_time": "2026-01-01",
            "article:modified_time": "2026-01-02",
        }
    }
    out = await backfill_summary_metadata(
        summary,
        request_id=9,
        content_text="ignored",
        correlation_id="cid-early",
        request_repo=_ExplodingRequestRepo(),  # type: ignore[arg-type]
        crawl_repo=_FakeCrawlRepo(crawl_row),
    )
    assert out["metadata"]["title"] == "T"
    assert out["metadata"]["domain"] == "e.example"


@pytest.mark.asyncio
async def test_backfill_non_string_scalar_metadata_is_not_blank() -> None:
    """``_is_blank`` evaluates a non-string scalar via ``str(value).strip()``
    (line 210): a present scalar like ``0`` is NOT blank, so a populated field is
    left untouched while a genuinely-blank sibling is still backfilled."""
    summary: dict[str, Any] = {"metadata": {"published_at": 0, "title": "   "}}
    out = await backfill_summary_metadata(
        summary,
        request_id=10,
        content_text="# Heading Fills Title",
        correlation_id="cid-scalar",
        request_repo=_FakeRequestRepo(None),
        crawl_repo=_FakeCrawlRepo(None),
    )
    # published_at=0 is treated as present (not blank) -> left as-is.
    assert out["metadata"]["published_at"] == 0
    # title was whitespace-only (blank) -> backfilled from the heading.
    assert out["metadata"]["title"] == "Heading Fills Title"


@pytest.mark.asyncio
async def test_backfill_swallows_repo_failures() -> None:
    """Best-effort: a crawl/request repo raising is logged and skipped, not raised."""

    class _BoomCrawl:
        async def async_get_crawl_result_by_request(self, request_id: int) -> Any:
            raise RuntimeError("crawl down")

    class _BoomRequest:
        async def async_get_request_by_id(self, request_id: int) -> Any:
            raise RuntimeError("request down")

    summary: dict[str, Any] = {"metadata": {}}
    out = await backfill_summary_metadata(
        summary,
        request_id=4,
        content_text="# Survivable Heading",
        correlation_id="cid-boom",
        request_repo=_BoomRequest(),  # type: ignore[arg-type]
        crawl_repo=_BoomCrawl(),  # type: ignore[arg-type]
    )
    # The heading heuristic still ran despite both repos failing.
    assert out["metadata"]["title"] == "Survivable Heading"
