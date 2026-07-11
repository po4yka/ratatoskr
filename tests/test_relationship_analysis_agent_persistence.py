"""RelationshipAnalysisAgent must persist its LLM call to llm_calls (rule 3)
and handle the call's failure locally.

Previously _analyze_with_llm ran chat_structured with no llm_repo (no row
written) and no local try/except (the raw exception propagated). It now writes
an llm_calls row (endpoint=relationship_analysis) on success and failure and
degrades to None on failure instead of raising.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.relationship_analysis_agent import RelationshipAnalysisAgent

pytestmark = pytest.mark.no_network


def _article(request_id: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        request_id=request_id,
        title="Title",
        url="https://example.com/a",
        author=None,
        domain=None,
        topic_tags=[],
        entities=[],
        summary_250=None,
    )


def _agent(*, llm: MagicMock, llm_repo: MagicMock | None) -> RelationshipAnalysisAgent:
    agent = RelationshipAnalysisAgent.__new__(RelationshipAnalysisAgent)
    agent.name = "RelationshipAnalysisAgent"
    agent.correlation_id = "cid"
    agent.logger = MagicMock()
    agent._llm = llm
    agent._llm_repo = llm_repo
    agent._load_prompt = lambda _lang: "prompt"  # type: ignore[method-assign]
    agent._parse_llm_response = lambda _data, _articles: SimpleNamespace(ok=True)  # type: ignore[method-assign]
    return agent


def _success_llm() -> MagicMock:
    result = SimpleNamespace(
        parsed=SimpleNamespace(model_dump=dict),
        model_used="anthropic/claude",
        tokens_prompt=150,
        tokens_completion=40,
        cost_usd=0.015,
        latency_ms=260,
    )
    llm = MagicMock()
    llm._model = "openrouter/default"
    llm.chat_structured = AsyncMock(return_value=result)
    return llm


@pytest.mark.asyncio
async def test_persists_llm_call_on_success() -> None:
    llm = _success_llm()
    repo = MagicMock()
    repo.async_insert_llm_call = AsyncMock(return_value=1)
    agent = _agent(llm=llm, llm_repo=repo)

    out = await agent._analyze_with_llm([_article(7), _article(8)], "en")

    assert out is not None and out.ok is True
    repo.async_insert_llm_call.assert_awaited_once()
    payload = repo.async_insert_llm_call.await_args.args[0]
    assert payload["request_id"] == 7
    assert payload["endpoint"] == "relationship_analysis"
    assert payload["status"] == "success"
    assert payload["model"] == "anthropic/claude"
    assert payload["cost_usd"] == 0.015


@pytest.mark.asyncio
async def test_handles_failure_locally_and_persists_error_row() -> None:
    llm = MagicMock()
    llm._model = "openrouter/default"
    llm.chat_structured = AsyncMock(side_effect=RuntimeError("rel boom"))
    repo = MagicMock()
    repo.async_insert_llm_call = AsyncMock(return_value=2)
    agent = _agent(llm=llm, llm_repo=repo)

    # New behavior: degrades to None instead of propagating the exception.
    out = await agent._analyze_with_llm([_article(7), _article(8)], "en")

    assert out is None
    repo.async_insert_llm_call.assert_awaited_once()
    payload = repo.async_insert_llm_call.await_args.args[0]
    assert payload["status"] == "error"
    assert "rel boom" in payload["error_text"]


@pytest.mark.asyncio
async def test_persists_without_request_id() -> None:
    llm = _success_llm()
    repo = MagicMock()
    repo.async_insert_llm_call = AsyncMock()
    agent = _agent(llm=llm, llm_repo=repo)

    out = await agent._analyze_with_llm([_article(None), _article(None)], "en")

    assert out is not None and out.ok is True
    repo.async_insert_llm_call.assert_awaited_once()
    assert repo.async_insert_llm_call.await_args.args[0]["request_id"] is None


@pytest.mark.asyncio
async def test_persist_failure_does_not_break_analysis() -> None:
    llm = _success_llm()
    repo = MagicMock()
    repo.async_insert_llm_call = AsyncMock(side_effect=RuntimeError("db down"))
    agent = _agent(llm=llm, llm_repo=repo)

    out = await agent._analyze_with_llm([_article(7), _article(8)], "en")

    assert out is not None and out.ok is True
    repo.async_insert_llm_call.assert_awaited_once()
