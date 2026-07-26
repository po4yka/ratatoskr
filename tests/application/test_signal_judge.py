"""Tests for bounded LLM-as-judge signal scoring."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.adapter_models.llm.llm_models import LLMCallResult, StructuredLLMResult
from app.application.services.signal_judge import SignalJudgeService
from app.core.call_status import CallStatus


class _FakeLLM:
    provider_name = "fake"

    def __init__(self, response_text: str, *, cost_usd: float | None = 0.01) -> None:
        self.calls: list[dict] = []
        self._response_text = response_text
        self._cost_usd = cost_usd

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return LLMCallResult(
            status=CallStatus.OK,
            response_text=self._response_text,
            cost_usd=self._cost_usd,
            latency_ms=123,
            model="test-model",
        )

    async def chat_structured(self, messages, *, response_model, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        data = json.loads(self._response_text)
        parsed = response_model(**data)
        return StructuredLLMResult(
            parsed=parsed,
            cost_usd=self._cost_usd,
            latency_ms=123,
            model_used="test-model",
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_signal_judge_calls_llm_only_for_capped_candidates() -> None:
    llm = _FakeLLM('{"relevance_score": 0.8, "decision": "queue", "reason": "useful"}')
    service = SignalJudgeService(llm_client=llm, daily_budget_usd=1.0)
    candidates = [
        SimpleNamespace(
            feed_item_id=1,
            should_reach_llm_judge=True,
            evidence={},
            score=0.7,
        ),
        SimpleNamespace(
            feed_item_id=2,
            should_reach_llm_judge=False,
            evidence={},
            score=0.4,
        ),
    ]
    rows = {
        1: {"title": "Useful post", "content_text": "body"},
        2: {"title": "Ignored post", "content_text": "body"},
    }

    judged = await service.judge(candidates, rows_by_item_id=rows)

    assert len(llm.calls) == 1
    assert judged[1].llm_score == 0.8
    assert judged[1].decision == "queue"
    assert 2 not in judged


@pytest.mark.asyncio
async def test_signal_judge_stops_at_daily_budget() -> None:
    llm = _FakeLLM('{"relevance_score": 0.8, "decision": "queue", "reason": "useful"}')
    service = SignalJudgeService(llm_client=llm, daily_budget_usd=0.0)
    candidates = [
        SimpleNamespace(feed_item_id=1, should_reach_llm_judge=True, evidence={}, score=0.7)
    ]

    judged = await service.judge(candidates, rows_by_item_id={1: {"title": "x"}})

    assert judged == {}
    assert llm.calls == []


@pytest.mark.asyncio
async def test_signal_judge_skips_candidate_on_llm_error() -> None:
    """When chat_structured raises, the candidate is skipped (exception caught in _judge_one)."""

    class ErrorLLM(_FakeLLM):
        async def chat_structured(self, messages, *, response_model, **kwargs):
            self.calls.append({"messages": messages, **kwargs})
            raise ValueError("LLM unavailable")

    llm = ErrorLLM("")
    service = SignalJudgeService(llm_client=llm, daily_budget_usd=1.0)

    judged = await service.judge(
        [SimpleNamespace(feed_item_id=1, should_reach_llm_judge=True, evidence={}, score=0.7)],
        rows_by_item_id={1: {"title": "x"}},
    )

    assert len(llm.calls) == 1
    assert judged == {}


class _RecordingLLMRepo:
    """Capture every persisted LLM-call payload (mirrors LLMRepositoryPort)."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    async def async_insert_llm_call(self, record: dict) -> int:
        self.records.append(record)
        return len(self.records)


@pytest.mark.asyncio
async def test_signal_judge_persists_successful_llm_call() -> None:
    llm = _FakeLLM('{"relevance_score": 0.8, "decision": "queue", "reason": "useful"}')
    repo = _RecordingLLMRepo()
    service = SignalJudgeService(llm_client=llm, llm_repo=repo, daily_budget_usd=1.0)

    judged = await service.judge(
        [SimpleNamespace(feed_item_id=1, should_reach_llm_judge=True, evidence={}, score=0.7)],
        rows_by_item_id={1: {"title": "Useful", "content_text": "body"}},
    )

    assert judged[1].decision == "queue"
    assert len(repo.records) == 1
    record = repo.records[0]
    assert record["endpoint"] == "signal_judge"
    assert record["status"] == "success"
    assert record["model"] == "test-model"
    assert record["cost_usd"] == 0.01
    assert record["latency_ms"] == 123
    assert record["structured_output_used"] is True


@pytest.mark.asyncio
async def test_signal_judge_persists_failed_llm_call() -> None:
    class ErrorLLM(_FakeLLM):
        async def chat_structured(self, messages, *, response_model, **kwargs):
            self.calls.append({"messages": messages, **kwargs})
            raise ValueError("LLM unavailable")

    llm = ErrorLLM("")
    repo = _RecordingLLMRepo()
    service = SignalJudgeService(llm_client=llm, llm_repo=repo, daily_budget_usd=1.0)

    judged = await service.judge(
        [SimpleNamespace(feed_item_id=1, should_reach_llm_judge=True, evidence={}, score=0.7)],
        rows_by_item_id={1: {"title": "x"}},
    )

    assert judged == {}
    assert len(repo.records) == 1
    record = repo.records[0]
    assert record["endpoint"] == "signal_judge"
    assert record["status"] == "error"
    assert record["error_text"] == "LLM unavailable"


@pytest.mark.asyncio
async def test_signal_judge_without_repo_still_scores() -> None:
    """Persistence is best-effort: a None repo must not break judging."""
    llm = _FakeLLM('{"relevance_score": 0.8, "decision": "queue", "reason": "useful"}')
    service = SignalJudgeService(llm_client=llm, daily_budget_usd=1.0)

    judged = await service.judge(
        [SimpleNamespace(feed_item_id=1, should_reach_llm_judge=True, evidence={}, score=0.7)],
        rows_by_item_id={1: {"title": "Useful", "content_text": "body"}},
    )

    assert judged[1].decision == "queue"
