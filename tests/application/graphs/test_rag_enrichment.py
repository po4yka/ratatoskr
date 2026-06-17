"""Tests for the content-only summary-completion port (audit #5).

Covers the two legacy ``ensure_summary_payload`` steps the content-only graph path
lost and now restores via the port-safe app service:

1. ``enrich_summary_rag_fields`` -- pure RAG-field enrichment (no LLM, no DB).
2. ``complete_summary_metadata_via_llm`` -- LLM metadata-completion via the
   ``LLMClientProtocol`` port.

All CI-safe (no langgraph / DB).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.application.services.summarization.rag_enrichment import (
    LLM_METADATA_FIELDS,
    complete_summary_metadata_via_llm,
    enrich_summary_rag_fields,
)
from app.core.call_status import CallStatus

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Step 1 -- pure RAG-field enrichment
# --------------------------------------------------------------------------- #
async def test_rag_enrichment_attaches_all_retrieval_fields() -> None:
    summary: dict[str, Any] = {
        "summary_250": "A short summary about distributed systems.",
        "summary_1000": "A much longer summary discussing consensus and replication in depth.",
        "tldr": "distributed systems tldr",
        "topic_tags": ["#systems", "consensus"],
    }
    content = "Distributed systems coordinate state across many machines. " * 40

    out = await enrich_summary_rag_fields(
        summary, content_text=content, chosen_lang="en", request_id=None
    )

    # Every RAG field the legacy enrich_with_rag_fields produced must be present.
    assert out["language"] == "en"
    assert out.get("semantic_boosters"), "semantic_boosters must be populated"
    assert out.get("query_expansion_keywords"), "query_expansion_keywords must be populated"
    assert out.get("semantic_chunks"), "semantic_chunks must be populated"
    # Chunk shape parity (article_id/language/topics/text/local_summary/local_keywords).
    chunk = out["semantic_chunks"][0]
    assert set(chunk) >= {"text", "local_summary", "local_keywords", "language", "topics"}
    assert chunk["language"] == "en"


async def test_rag_enrichment_seeds_article_id_from_request_id() -> None:
    summary: dict[str, Any] = {"summary_250": "s", "summary_1000": "l", "tldr": "t"}
    out = await enrich_summary_rag_fields(
        summary, content_text="body text here", chosen_lang="en", request_id=99
    )
    assert out["article_id"] == "99"


async def test_rag_enrichment_preserves_existing_fields() -> None:
    summary: dict[str, Any] = {
        "summary_250": "s",
        "summary_1000": "l",
        "tldr": "t",
        "semantic_boosters": ["already here"],
        "query_expansion_keywords": [f"kw{i}" for i in range(25)],
    }
    out = await enrich_summary_rag_fields(
        summary, content_text="body", chosen_lang="en", request_id=None
    )
    # Existing boosters are kept (capped, not regenerated from scratch).
    assert "already here" in out["semantic_boosters"]
    # 25 existing keywords (>=20) are kept (capped to 30), not replaced.
    assert len(out["query_expansion_keywords"]) == 25


# --------------------------------------------------------------------------- #
# Step 2 -- LLM metadata-completion via the port
# --------------------------------------------------------------------------- #
def _llm_ok(parsed: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        status=CallStatus.OK,
        model="meta-model",
        response_text=None,
        response_json={"choices": [{"message": {"parsed": parsed}}]},
        tokens_prompt=10,
        tokens_completion=4,
        cost_usd=None,
        latency_ms=120,
        error_text=None,
    )


async def test_metadata_completion_fills_missing_fields_via_port() -> None:
    llm = SimpleNamespace(
        chat=AsyncMock(return_value=_llm_ok({"title": "Real Title", "author": "Jane Doe"}))
    )
    completed, record = await complete_summary_metadata_via_llm(
        llm_client=llm,
        content_text="Some article body about a topic worth summarizing.",
        fields=["title", "author"],
        request_id=None,
        correlation_id="cid-meta",
        structured_output_mode="json_schema",
    )
    assert completed == {"title": "Real Title", "author": "Jane Doe"}
    # A serializable llm_calls record is returned for persist-everything.
    assert record is not None
    assert record["attempt_trigger"] == "graph_node"
    assert record["model"] == "meta-model"
    assert record["status"] == "ok"
    # content-only path -> no request row.
    assert record["request_id"] is None
    llm.chat.assert_awaited_once()


async def test_metadata_completion_no_call_when_no_missing_fields() -> None:
    llm = SimpleNamespace(chat=AsyncMock())
    completed, record = await complete_summary_metadata_via_llm(
        llm_client=llm,
        content_text="body",
        fields=[],
        request_id=None,
        correlation_id="c",
    )
    assert completed == {}
    assert record is None
    llm.chat.assert_not_awaited()


async def test_metadata_completion_no_call_when_content_empty() -> None:
    llm = SimpleNamespace(chat=AsyncMock())
    completed, record = await complete_summary_metadata_via_llm(
        llm_client=llm,
        content_text="   ",
        fields=["title"],
        request_id=None,
        correlation_id="c",
    )
    assert completed == {}
    assert record is None
    llm.chat.assert_not_awaited()


async def test_metadata_completion_swallows_transport_error() -> None:
    llm = SimpleNamespace(chat=AsyncMock(side_effect=RuntimeError("provider down")))
    completed, record = await complete_summary_metadata_via_llm(
        llm_client=llm,
        content_text="body text",
        fields=["title"],
        request_id=None,
        correlation_id="c",
    )
    # Transport failure -> empty result, no record (the adapter already persisted it).
    assert completed == {}
    assert record is None


async def test_metadata_completion_targets_are_the_llm_fields() -> None:
    # Guard the field set the facade hands in matches the legacy LLM target list.
    assert LLM_METADATA_FIELDS == ("title", "author", "published_at", "last_updated")
