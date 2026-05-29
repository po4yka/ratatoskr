from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.application.services.summarization.llm_response_workflow_storage import (
    LLMWorkflowStorageMixin,
)
from app.core.call_status import CallStatus
from app.observability import metrics


class _Repo:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def async_insert_llm_call(self, record: dict) -> int:
        self.calls.append(record)
        return len(self.calls)


class _Storage(LLMWorkflowStorageMixin):
    def __init__(self) -> None:
        self._db_write_queue = None
        self.llm_repo = _Repo()
        self.cfg = SimpleNamespace(
            openrouter=SimpleNamespace(model="test/default"),
            llm_usage_budget=SimpleNamespace(
                max_tokens_per_request=None,
                max_cost_usd_per_request=None,
            ),
        )


@pytest.mark.asyncio
async def test_persist_llm_call_increments_usage_metrics() -> None:
    if not metrics.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client unavailable")

    storage = _Storage()
    llm = SimpleNamespace(
        status=CallStatus.OK,
        model="test/model",
        endpoint="/chat",
        request_headers={},
        request_messages=[],
        response_text="{}",
        response_json={},
        tokens_prompt=11,
        tokens_completion=7,
        cost_usd=0.003,
        latency_ms=250,
        error_text=None,
        structured_output_used=True,
        structured_output_mode="json_schema",
        error_context=None,
        per_model_attempts=[],
    )

    # "test/model" is not in the configured allowlist, so it is bucketed to
    # "other" by _bucket_model().  Assertions use "other" intentionally.
    calls_before = metrics.LLM_CALL_ATTEMPTS_TOTAL.labels(
        provider="openrouter",
        model="other",
        status="ok",
    )._value.get()
    prompt_before = metrics.LLM_TOKENS_TOTAL.labels(
        provider="openrouter",
        model="other",
        type="prompt",
    )._value.get()
    cost_before = metrics.LLM_COST_USD_TOTAL.labels(
        provider="openrouter",
        model="other",
    )._value.get()

    await storage._persist_llm_call(llm, req_id=123, correlation_id="cid")

    assert len(storage.llm_repo.calls) == 1
    assert (
        metrics.LLM_CALL_ATTEMPTS_TOTAL.labels(
            provider="openrouter",
            model="other",
            status="ok",
        )._value.get()
        == calls_before + 1
    )
    assert (
        metrics.LLM_TOKENS_TOTAL.labels(
            provider="openrouter",
            model="other",
            type="prompt",
        )._value.get()
        == prompt_before + 11
    )
    assert metrics.LLM_COST_USD_TOTAL.labels(
        provider="openrouter",
        model="other",
    )._value.get() == pytest.approx(cost_before + 0.003)
