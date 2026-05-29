"""Tests for per-model cascade attempt persistence in LLMWorkflowStorageMixin."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.services.summarization.llm_response_workflow_storage import (
    LLMWorkflowStorageMixin,
)
from app.core.call_status import CallStatus


def _make_storage_mixin(db_write_queue: Any = None) -> LLMWorkflowStorageMixin:
    """Construct a minimal LLMWorkflowStorageMixin instance."""
    instance = LLMWorkflowStorageMixin.__new__(LLMWorkflowStorageMixin)
    instance._db_write_queue = db_write_queue
    instance.cfg = MagicMock()
    instance.cfg.openrouter.model = "primary-model"
    instance.llm_repo = AsyncMock()
    instance.llm_repo.async_insert_llm_call = AsyncMock()
    instance.llm_repo.async_insert_llm_calls_batch = AsyncMock()
    return instance


def _make_llm(
    model: str = "final-model",
    status: CallStatus = CallStatus.OK,
    per_model_attempts: list[dict[str, Any]] | None = None,
) -> MagicMock:
    llm = MagicMock()
    llm.model = model
    llm.status = status
    llm.endpoint = "/api/v1/chat/completions"
    llm.request_headers = {}
    llm.request_messages = []
    llm.response_text = "ok"
    llm.response_json = {}
    llm.tokens_prompt = 10
    llm.tokens_completion = 5
    llm.cost_usd = 0.001
    llm.latency_ms = 200
    llm.error_text = None
    llm.error_context = None
    llm.structured_output_used = False
    llm.structured_output_mode = None
    llm.per_model_attempts = per_model_attempts or []
    return llm


class TestBuildCascadeAttemptPayload:
    def test_returns_expected_fields(self) -> None:
        storage = _make_storage_mixin()
        llm = _make_llm(model="final-model")
        attempt: dict[str, Any] = {
            "model": "fallback-model",
            "status": "timeout",
            "latency_ms": 90000,
            "error_text": "Model fallback-model timed out after 90s.",
            "error_context": {"timeout": True},
            "per_model_timeout_sec": 90.0,
        }

        payload = storage._build_cascade_attempt_payload(llm, req_id=42, attempt=attempt)

        assert payload["request_id"] == 42
        assert payload["model"] == "fallback-model"
        assert payload["provider"] == "openrouter"
        assert payload["status"] == "error"
        assert payload["attempt_trigger"] == "auto_backfill"
        assert payload["latency_ms"] == 90000
        assert payload["error_text"] == "Model fallback-model timed out after 90s."
        assert payload["error_context_json"] == {"timeout": True}

    def test_falls_back_to_llm_model_when_attempt_lacks_model(self) -> None:
        storage = _make_storage_mixin()
        llm = _make_llm(model="primary-model")
        attempt: dict[str, Any] = {
            "model": None,
            "status": "error",
            "latency_ms": 5000,
            "error_text": "some error",
            "error_context": None,
            "per_model_timeout_sec": 90.0,
        }

        payload = storage._build_cascade_attempt_payload(llm, req_id=7, attempt=attempt)

        assert payload["model"] == "primary-model"
        assert payload["status"] == "error"
        assert payload["attempt_trigger"] == "auto_backfill"

    @pytest.mark.asyncio
    async def test_persist_llm_call_enqueues_n_plus_1_when_cascade_has_n_entries(
        self,
    ) -> None:
        """_persist_llm_call should enqueue one entry per cascade attempt before the terminal."""
        enqueued: list[dict[str, Any]] = []

        async def _fake_enqueue_batch(
            payload: dict[str, Any],
            *,
            batch_key: str,
            execute_batch: Any,
            operation_name: str,
            correlation_id: str,
        ) -> None:
            enqueued.append({"payload": payload, "operation_name": operation_name})

        mock_queue = MagicMock()
        mock_queue.enqueue_batch = _fake_enqueue_batch

        storage = _make_storage_mixin(db_write_queue=mock_queue)
        cascade = [
            {
                "model": "qwen/qwen3.6-flash",
                "status": "timeout",
                "latency_ms": 90000,
                "error_text": "timed out",
                "error_context": {"timeout": True},
                "per_model_timeout_sec": 90.0,
            },
            {
                "model": "qwen/qwen3.6-plus-04-02",
                "status": "error",
                "latency_ms": 45000,
                "error_text": "api error",
                "error_context": None,
                "per_model_timeout_sec": 90.0,
            },
        ]
        llm = _make_llm(model="moonshotai/kimi-k2-0905", per_model_attempts=cascade)

        await storage._persist_llm_call(llm, req_id=99, correlation_id="abc123")

        # 2 cascade entries + 1 terminal = 3 enqueue calls
        assert len(enqueued) == 3
        cascade_ops = [e["operation_name"] for e in enqueued]
        assert cascade_ops[0] == "persist_llm_call_cascade"
        assert cascade_ops[1] == "persist_llm_call_cascade"
        assert cascade_ops[2] == "persist_llm_call"

    @pytest.mark.asyncio
    async def test_persist_llm_call_inserts_single_row_when_no_cascade(self) -> None:
        """When per_model_attempts is empty, only the terminal row is inserted."""
        enqueued: list[dict[str, Any]] = []

        async def _fake_enqueue_batch(
            payload: dict[str, Any],
            *,
            batch_key: str,
            execute_batch: Any,
            operation_name: str,
            correlation_id: str,
        ) -> None:
            enqueued.append({"payload": payload, "operation_name": operation_name})

        mock_queue = MagicMock()
        mock_queue.enqueue_batch = _fake_enqueue_batch

        storage = _make_storage_mixin(db_write_queue=mock_queue)
        llm = _make_llm(model="primary-model", per_model_attempts=[])

        await storage._persist_llm_call(llm, req_id=1, correlation_id="cid1")

        assert len(enqueued) == 1
        assert enqueued[0]["operation_name"] == "persist_llm_call"
