"""WebSearchAgent must persist its analysis LLM call to llm_calls (rule 3).

The content-analysis chat_structured call was previously invisible to the DB,
so its cost/tokens were unrecoverable. The agent now writes an llm_calls row
(endpoint=web_search_analysis) against the summarize request, on success and on
failure, when the DI layer supplied an llm_repo + request_id.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.web_search_agent import SearchAnalysisResult, WebSearchAgent

pytestmark = pytest.mark.no_network


def _agent(*, llm: MagicMock, llm_repo: MagicMock | None, request_id: int | None) -> WebSearchAgent:
    agent = WebSearchAgent.__new__(WebSearchAgent)
    agent.correlation_id = "cid"
    agent._llm = llm
    agent._llm_repo = llm_repo
    agent._request_id = request_id
    agent._load_analysis_prompt = lambda _lang: "system prompt"  # type: ignore[method-assign]
    return agent


def _success_llm() -> MagicMock:
    parsed = SearchAnalysisResult(needs_search=True, queries=["q1"], reason="ok")
    result = SimpleNamespace(
        parsed=parsed,
        model_used="anthropic/claude",
        tokens_prompt=120,
        tokens_completion=30,
        cost_usd=0.0042,
        latency_ms=210,
    )
    llm = MagicMock()
    llm._model = "openrouter/default"
    llm.chat_structured = AsyncMock(return_value=result)
    llm._success_result = result
    return llm


@pytest.mark.asyncio
async def test_persists_llm_call_on_success() -> None:
    llm = _success_llm()
    repo = MagicMock()
    repo.async_insert_llm_call = AsyncMock(return_value=1)
    agent = _agent(llm=llm, llm_repo=repo, request_id=42)

    out = await agent._analyze_content("a" * 200, "en", "cid")

    assert out is llm._success_result.parsed
    repo.async_insert_llm_call.assert_awaited_once()
    payload = repo.async_insert_llm_call.await_args.args[0]
    assert payload["request_id"] == 42
    assert payload["endpoint"] == "web_search_analysis"
    assert payload["status"] == "success"
    assert payload["provider"] == "anthropic"
    assert payload["model"] == "anthropic/claude"
    assert payload["tokens_prompt"] == 120
    assert payload["tokens_completion"] == 30
    assert payload["cost_usd"] == 0.0042
    assert payload["structured_output_used"] is True


@pytest.mark.asyncio
async def test_persists_error_row_and_reraises_on_failure() -> None:
    llm = MagicMock()
    llm._model = "openrouter/default"
    llm.chat_structured = AsyncMock(side_effect=RuntimeError("llm boom"))
    repo = MagicMock()
    repo.async_insert_llm_call = AsyncMock(return_value=2)
    agent = _agent(llm=llm, llm_repo=repo, request_id=42)

    with pytest.raises(RuntimeError, match="llm boom"):
        await agent._analyze_content("a" * 200, "en", "cid")

    repo.async_insert_llm_call.assert_awaited_once()
    payload = repo.async_insert_llm_call.await_args.args[0]
    assert payload["request_id"] == 42
    assert payload["status"] == "error"
    assert "llm boom" in payload["error_text"]


@pytest.mark.asyncio
async def test_persists_when_request_id_missing() -> None:
    llm = _success_llm()
    repo = MagicMock()
    repo.async_insert_llm_call = AsyncMock()
    agent = _agent(llm=llm, llm_repo=repo, request_id=None)

    out = await agent._analyze_content("a" * 200, "en", "cid")

    assert out is llm._success_result.parsed
    repo.async_insert_llm_call.assert_awaited_once()
    assert repo.async_insert_llm_call.await_args.args[0]["request_id"] is None


@pytest.mark.asyncio
async def test_persist_failure_does_not_break_enrichment() -> None:
    llm = _success_llm()
    repo = MagicMock()
    repo.async_insert_llm_call = AsyncMock(side_effect=RuntimeError("db down"))
    agent = _agent(llm=llm, llm_repo=repo, request_id=42)

    # A persistence failure is logged, never propagated.
    out = await agent._analyze_content("a" * 200, "en", "cid")

    assert out is llm._success_result.parsed
    repo.async_insert_llm_call.assert_awaited_once()
