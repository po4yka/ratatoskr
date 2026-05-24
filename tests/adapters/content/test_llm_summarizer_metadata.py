from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.content.llm_summarizer_metadata import LLMSummaryMetadataHelper
from app.core.call_status import CallStatus


class _RequestRepo:
    def __init__(self, row: dict[str, Any] | None = None, exc: Exception | None = None) -> None:
        self.row = row
        self.exc = exc

    async def async_get_request_by_id(self, req_id: int) -> dict[str, Any] | None:
        if self.exc:
            raise self.exc
        return self.row


class _CrawlRepo:
    def __init__(self, row: dict[str, Any] | None = None, exc: Exception | None = None) -> None:
        self.row = row
        self.exc = exc

    async def async_get_crawl_result_by_request(self, req_id: int) -> dict[str, Any] | None:
        if self.exc:
            raise self.exc
        return self.row


class _SemanticHelper:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enrich_with_rag_fields(self, summary: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        summary["rag"] = True
        return summary


class _Workflow:
    def __init__(self) -> None:
        self.persisted: list[object] = []

    async def persist_llm_call(self, llm: object, req_id: int, correlation_id: str | None) -> None:
        self.persisted.append(llm)


class _OpenRouter:
    def __init__(self, llm: object | None = None, exc: Exception | None = None) -> None:
        self.llm = llm
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> object:
        self.calls.append({"messages": messages, **kwargs})
        if self.exc:
            raise self.exc
        return self.llm


@asynccontextmanager
async def _sem() -> Any:
    yield


def _helper(
    *,
    request_row: dict[str, Any] | None = None,
    crawl_row: dict[str, Any] | None = None,
    llm: object | None = None,
    openrouter_exc: Exception | None = None,
) -> tuple[LLMSummaryMetadataHelper, _SemanticHelper, _Workflow, _OpenRouter]:
    semantic = _SemanticHelper()
    workflow = _Workflow()
    openrouter = _OpenRouter(llm, openrouter_exc)
    helper = LLMSummaryMetadataHelper(
        request_repo=_RequestRepo(request_row),
        crawl_result_repo=_CrawlRepo(crawl_row),
        openrouter=openrouter,
        workflow=workflow,
        sem=lambda: _sem(),
        semantic_helper=semantic,
    )
    return helper, semantic, workflow, openrouter


@pytest.mark.asyncio
async def test_ensure_summary_metadata_returns_early_when_complete() -> None:
    helper, semantic, _workflow, _openrouter = _helper()
    summary = {
        "metadata": {
            "title": "T",
            "canonical_url": "https://example.test/a",
            "domain": "example.test",
            "author": "A",
            "published_at": "2026-01-01",
            "last_updated": "2026-01-02",
        }
    }

    result = await helper.ensure_summary_metadata(summary, 1, "content", "cid")

    assert result is summary
    assert semantic.calls == []


@pytest.mark.asyncio
async def test_ensure_summary_metadata_backfills_from_crawl_request_heading_and_llm() -> None:
    llm = SimpleNamespace(
        status=CallStatus.OK,
        error_text=None,
        response_json={
            "choices": [
                {
                    "message": {
                        "parsed": '{"author": "LLM Author", "published_at": "2026-01-01", "last_updated": null}'
                    }
                }
            ]
        },
        response_text=None,
    )
    helper, semantic, workflow, openrouter = _helper(
        request_row={"normalized_url": "https://example.test/article"},
        crawl_row={
            "metadata_json": {
                "meta": [
                    {"property": "og:url", "content": "https://canonical.test/story"},
                    {"name": "article:modified_time", "content": "2026-01-02"},
                ]
            }
        },
        llm=llm,
    )
    summary: dict[str, Any] = {"metadata": {"domain": ""}}

    result = await helper.ensure_summary_metadata(
        summary,
        1,
        "# Heading Title\n\nArticle body",
        "cid",
        chosen_lang="en",
    )

    metadata = result["metadata"]
    assert metadata["canonical_url"] == "https://canonical.test/story"
    assert metadata["domain"] == "canonical.test"
    assert metadata["title"] == "Heading Title"
    assert metadata["author"] == "LLM Author"
    assert metadata["published_at"] == "2026-01-01"
    assert metadata["last_updated"] == "2026-01-02"
    assert result["rag"] is True
    assert workflow.persisted == [llm]
    assert openrouter.calls[0]["request_id"] == 1
    assert semantic.calls[0]["chosen_lang"] == "en"


@pytest.mark.asyncio
async def test_load_firecrawl_metadata_accepts_raw_response_json_and_bad_json() -> None:
    helper, _semantic, _workflow, _openrouter = _helper(
        crawl_row={
            "metadata_json": "{bad",
            "raw_response_json": '{"data": {"metadata": {"title": "Raw title", "author": "Raw author"}}}',
        }
    )

    assert await helper._load_firecrawl_metadata(1) == {
        "title": "Raw title",
        "author": "Raw author",
    }

    helper, _semantic, _workflow, _openrouter = _helper(crawl_row={"raw_response_json": "{bad"})
    assert await helper._load_firecrawl_metadata(1) == {}


def test_metadata_helper_pure_parsers_and_extractors() -> None:
    helper, _semantic, _workflow, _openrouter = _helper()
    collector: dict[str, str] = {}

    helper._flatten_metadata_values(
        [
            {"property": "og:title", "content": "OG Title"},
            {"nested": {"name": "author", "value": "Author"}},
            {"title": "Direct title"},
            None,
            "ignored",
        ],
        collector,
    )

    assert collector["og:title"] == "OG Title"
    assert collector["author"] == "Author"
    assert collector["title"] == "Direct title"
    assert helper._is_blank("  ")
    assert not helper._is_blank(1)
    assert helper._extract_heading_title("Title: Explicit title | metadata") == "Explicit title"
    assert helper._extract_heading_title("[source: x]\nDuration: 1m\nLead line") == "Lead line"
    assert helper._extract_heading_title("") is None
    assert helper._parse_metadata_completion({"choices": [{"message": {"parsed": {"title": "T"}}}]}, None) == {
        "title": "T"
    }
    assert helper._parse_metadata_completion(
        {"choices": [{"message": {"content": "prefix {\"title\":\"T\"}"}}]},
        None,
    ) == {"title": "T"}
    assert helper._parse_metadata_completion(None, "prefix {\"author\":\"A\"}") == {"author": "A"}


@pytest.mark.asyncio
async def test_generate_metadata_completion_handles_failures() -> None:
    helper, _semantic, _workflow, _openrouter = _helper(openrouter_exc=RuntimeError("down"))
    assert await helper._generate_metadata_completion("content", ["title"], 1, "cid") == {}
    assert await helper._generate_metadata_completion("", ["title"], 1, "cid") == {}
    assert await helper._generate_metadata_completion("content", [], 1, "cid") == {}

    failed_llm = SimpleNamespace(
        status=CallStatus.ERROR,
        error_text="bad",
        response_json=None,
        response_text=None,
    )
    helper, _semantic, workflow, _openrouter = _helper(llm=failed_llm)
    assert await helper._generate_metadata_completion("content", ["title"], 1, "cid") == {}
    assert workflow.persisted == [failed_llm]
