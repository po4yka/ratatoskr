import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.adapters.content.llm_response_workflow import (
    LLMInteractionConfig,
    LLMRepairContext,
    LLMRequestConfig,
    LLMResponseWorkflow,
    LLMSummaryPersistenceSettings,
    LLMWorkflowNotifications,
)


class _DummySemaphore:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _StrictFakeLLMClient:
    provider_name = "strict-fake"

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stream: bool = False,
        request_id: int | None = None,
        response_format: dict[str, Any] | None = None,
        model_override: str | None = None,
        fallback_models_override: tuple[str, ...] | list[str] | None = None,
        on_stream_delta: Any | None = None,
        per_model_timeout_sec: float | None = None,
        per_model_timeout_overrides: dict[str, float] | None = None,
        budget_tight_ratio: float = 0.6,
        truncation_max_count: int = 2,
    ) -> Any:
        self.calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "top_p": top_p,
                "stream": stream,
                "request_id": request_id,
                "response_format": response_format,
                "model_override": model_override,
                "fallback_models_override": fallback_models_override,
                "on_stream_delta": on_stream_delta,
                "per_model_timeout_sec": per_model_timeout_sec,
                "per_model_timeout_overrides": per_model_timeout_overrides,
                "budget_tight_ratio": budget_tight_ratio,
                "truncation_max_count": truncation_max_count,
            }
        )
        return self.result

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        *,
        response_model: type[Any],
        max_retries: int = 3,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        request_id: int | None = None,
        model_override: str | None = None,
        fallback_models_override: tuple[str, ...] | list[str] | None = None,
    ) -> Any:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def _workflow_repo_kwargs() -> dict[str, MagicMock]:
    return {
        "summary_repo": MagicMock(),
        "request_repo": MagicMock(),
        "llm_repo": MagicMock(),
        "user_repo": MagicMock(),
    }


class LLMResponseWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cfg = MagicMock()
        self.cfg.openrouter.model = "test-model"
        self.cfg.openrouter.fallback_models = ()
        self.cfg.openrouter.temperature = 0.1
        self.cfg.openrouter.top_p = 1.0
        self.cfg.openrouter.max_tokens = 4096
        self.cfg.openrouter.structured_output_mode = "json_object"
        # Mock runtime config timeouts used by semaphore/parsing wrappers
        self.cfg.runtime.semaphore_acquire_timeout_sec = 30.0
        self.cfg.runtime.llm_call_timeout_sec = 180.0
        self.cfg.runtime.llm_call_max_retries = 2
        self.cfg.runtime.json_parse_timeout_sec = 60.0
        self.cfg.runtime.llm_per_model_timeout_min_sec = 90.0
        self.cfg.runtime.llm_per_model_timeout_overrides = {}
        self.cfg.llm_usage_budget = None

        self.db = MagicMock()
        self.response_formatter = MagicMock()
        self.openrouter = MagicMock()

        self.workflow = LLMResponseWorkflow(
            cfg=self.cfg,
            db=self.db,
            llm_client=self.openrouter,
            response_formatter=self.response_formatter,
            audit_func=lambda *args, **kwargs: None,
            sem=lambda: _DummySemaphore(),
            **_workflow_repo_kwargs(),
        )

        # Mock repositories directly to avoid model/proxy issues
        # Store AsyncMock objects as typed instance variables for assertion access
        self.workflow.request_repo = MagicMock()
        self.update_status_mock: AsyncMock = AsyncMock()
        self.workflow.request_repo.async_update_request_status = self.update_status_mock

        self.workflow.summary_repo = MagicMock()
        self.upsert_summary_mock: AsyncMock = AsyncMock(return_value=1)
        self.workflow.summary_repo.async_finalize_request_summary = self.upsert_summary_mock

        self.workflow.llm_repo = MagicMock()
        self.insert_llm_call_mock: AsyncMock = AsyncMock(return_value=1)
        self.workflow.llm_repo.async_insert_llm_call = self.insert_llm_call_mock

        self.base_messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Please summarise"},
        ]

        self.request = LLMRequestConfig(
            messages=self.base_messages,
            response_format={"type": "json_object"},
            max_tokens=256,
            temperature=0.1,
            top_p=1.0,
        )

        self.repair_context = LLMRepairContext(
            base_messages=self.base_messages,
            repair_response_format={"type": "json_object"},
            repair_max_tokens=256,
            default_prompt="Repair",
        )

        self.completion_mock = AsyncMock()
        self.llm_error_mock = AsyncMock()
        self.repair_failure_mock = AsyncMock()
        self.parsing_failure_mock = AsyncMock()

        self.notifications = LLMWorkflowNotifications(
            completion=self.completion_mock,
            llm_error=self.llm_error_mock,
            repair_failure=self.repair_failure_mock,
            parsing_failure=self.parsing_failure_mock,
        )

        self.interaction = LLMInteractionConfig(interaction_id=None)
        self.persistence = LLMSummaryPersistenceSettings(lang="en", is_read=True)

    async def test_execute_success_persists_summary(self) -> None:
        summary_payload = {
            "summary_250": "Summary body",
            "tldr": "TLDR text",
        }
        llm_response = self._llm_response(summary_payload)
        self.openrouter.chat = AsyncMock(return_value=llm_response)

        with unittest.mock.patch(
            "app.adapters.content.llm_response_workflow.parse_summary_response",
            return_value=SimpleNamespace(
                shaped={"summary_250": "Summary body", "tldr": "TLDR text"},
                errors=[],
                used_local_fix=False,
            ),
        ):
            summary = await self.workflow.execute_summary_workflow(
                message=MagicMock(),
                req_id=101,
                correlation_id="cid",
                interaction_config=self.interaction,
                persistence=self.persistence,
                repair_context=self.repair_context,
                requests=[self.request],
                notifications=self.notifications,
            )

        assert summary is not None
        self.upsert_summary_mock.assert_awaited_once()
        _args, kwargs = self.upsert_summary_mock.await_args
        assert kwargs["request_id"] == 101
        assert kwargs["lang"] == "en"
        self.insert_llm_call_mock.assert_awaited_once()
        self.completion_mock.assert_awaited_once()
        self.llm_error_mock.assert_not_awaited()

    async def test_llm_call_cap_limits_total_invocations(self) -> None:
        """A flood of failing attempts must stop at the configured per-request cap."""
        self.cfg.runtime.llm_max_calls_per_request = 2
        self.openrouter.chat = AsyncMock(
            return_value=self._llm_response({}, status="error", error_text="boom", text=None)
        )

        # Five attempts requested, but the cap is 2.
        requests = [self.request.model_copy() for _ in range(5)]
        summary = await self.workflow.execute_summary_workflow(
            message=MagicMock(),
            req_id=777,
            correlation_id="cap",
            interaction_config=self.interaction,
            persistence=self.persistence,
            repair_context=self.repair_context,
            requests=requests,
            notifications=self.notifications,
        )

        assert summary is None
        # Only two cascades run despite five requested attempts; the workflow
        # then aborts cleanly via the all-attempts-failed path.
        assert self.openrouter.chat.await_count == 2
        self.llm_error_mock.assert_awaited()

    async def test_execute_accepts_strict_llm_client_protocol(self) -> None:
        summary_payload = {
            "summary_250": "Summary body",
            "tldr": "TLDR text",
        }
        fake_client = _StrictFakeLLMClient(self._llm_response(summary_payload))
        self.cfg.runtime.llm_per_model_timeout_overrides = {"fallback-model": 12.0}
        workflow = LLMResponseWorkflow(
            cfg=self.cfg,
            db=self.db,
            llm_client=fake_client,
            response_formatter=self.response_formatter,
            audit_func=lambda *args, **kwargs: None,
            sem=lambda: _DummySemaphore(),
            **_workflow_repo_kwargs(),
        )
        workflow.request_repo = self.workflow.request_repo
        workflow.summary_repo = self.workflow.summary_repo
        workflow.llm_repo = self.workflow.llm_repo
        workflow.user_repo = self.workflow.user_repo

        request = self.request.model_copy(
            update={
                "stream": True,
                "model_override": "primary-model",
                "fallback_models_override": ("fallback-model",),
            }
        )

        with unittest.mock.patch(
            "app.adapters.content.llm_response_workflow.parse_summary_response",
            return_value=SimpleNamespace(
                shaped=summary_payload,
                errors=[],
                used_local_fix=False,
            ),
        ):
            summary = await workflow.execute_summary_workflow(
                message=MagicMock(),
                req_id=111,
                correlation_id="strict",
                interaction_config=self.interaction,
                persistence=self.persistence,
                repair_context=self.repair_context,
                requests=[request],
                notifications=self.notifications,
            )

        assert summary is not None
        assert summary["summary_250"] == summary_payload["summary_250"]
        assert summary["tldr"] == summary_payload["tldr"]
        assert summary["summary_quality"]["model_used"] == "test-model"
        assert fake_client.calls == [
            {
                "messages": self.base_messages,
                "temperature": 0.1,
                "max_tokens": 256,
                "top_p": 1.0,
                "stream": True,
                "request_id": 111,
                "response_format": {"type": "json_object"},
                "model_override": "primary-model",
                "fallback_models_override": ("fallback-model",),
                "on_stream_delta": None,
                "per_model_timeout_sec": 90.0,
                "per_model_timeout_overrides": {"fallback-model": 12.0},
                "budget_tight_ratio": unittest.mock.ANY,
                "truncation_max_count": unittest.mock.ANY,
            }
        ]

    async def test_execute_runs_repair_on_parse_failure(self) -> None:
        llm_invalid = self._llm_response({}, text="not json")
        llm_repaired = self._llm_response({}, text='{"summary_250": "Fixed", "tldr": "TLDR"}')
        self.openrouter.chat = AsyncMock(side_effect=[llm_invalid, llm_repaired])

        with unittest.mock.patch(
            "app.adapters.content.llm_response_workflow.parse_summary_response",
            side_effect=[
                SimpleNamespace(shaped=None, errors=["invalid"], used_local_fix=False),
                SimpleNamespace(
                    shaped={"summary_250": "Fixed", "tldr": "TLDR"},
                    errors=[],
                    used_local_fix=False,
                ),
            ],
        ):
            summary = await self.workflow.execute_summary_workflow(
                message=MagicMock(),
                req_id=202,
                correlation_id="repair",
                interaction_config=self.interaction,
                persistence=self.persistence,
                repair_context=self.repair_context,
                requests=[self.request],
                notifications=self.notifications,
            )

        assert summary is not None
        assert self.openrouter.chat.await_count == 2
        self.repair_failure_mock.assert_not_awaited()
        self.upsert_summary_mock.assert_awaited_once()
        _args, kwargs = self.upsert_summary_mock.await_args
        quality = kwargs["json_payload"]["summary_quality"]
        assert quality["repair_attempted"] is True
        assert quality["repair_succeeded"] is True
        assert quality["structured_output_mode"] == "json_object"
        assert quality["model_used"] == "test-model"
        assert self.insert_llm_call_mock.await_count >= 1

    async def test_execute_handles_llm_error(self) -> None:
        llm_error = self._llm_response({}, status="error", error_text="boom", text=None)
        self.openrouter.chat = AsyncMock(return_value=llm_error)

        summary = await self.workflow.execute_summary_workflow(
            message=MagicMock(),
            req_id=303,
            correlation_id="err",
            interaction_config=self.interaction,
            persistence=self.persistence,
            repair_context=self.repair_context,
            requests=[self.request],
            notifications=self.notifications,
        )

        assert summary is None
        self.upsert_summary_mock.assert_not_awaited()
        self.update_status_mock.assert_awaited_with(303, "error")
        self.insert_llm_call_mock.assert_awaited_once()
        # llm_error callback is called twice: once for the error, once for all attempts failed
        assert self.llm_error_mock.await_count == 2

    async def test_empty_summary_counts_attempts_and_models(self) -> None:
        req_primary = LLMRequestConfig(
            messages=self.base_messages,
            response_format={"type": "json_object"},
            max_tokens=256,
            temperature=0.1,
            top_p=1.0,
            model_override="primary",
        )
        req_fallback = LLMRequestConfig(
            messages=self.base_messages,
            response_format={"type": "json_object"},
            max_tokens=256,
            temperature=0.1,
            top_p=1.0,
            model_override="fallback",
        )

        llm_empty_first = self._llm_response({})
        llm_empty_second = self._llm_response({})
        self.openrouter.chat = AsyncMock(
            side_effect=[
                llm_empty_first,  # Primary request
                llm_empty_first,  # Primary repair
                llm_empty_second,  # Fallback request
                llm_empty_second,  # Fallback repair
            ]
        )

        with (
            unittest.mock.patch(
                "app.adapters.content.llm_response_workflow.parse_summary_response",
                return_value=SimpleNamespace(
                    shaped={}, errors=["missing_summary_fields"], used_local_fix=False
                ),
            ),
            unittest.mock.patch.object(
                self.workflow,
                "_handle_all_attempts_failed",
                wraps=self.workflow._handle_all_attempts_failed,
                new_callable=AsyncMock,
            ) as fail_mock,
        ):
            summary = await self.workflow.execute_summary_workflow(
                message=MagicMock(),
                req_id=404,
                correlation_id="empty",
                interaction_config=self.interaction,
                persistence=self.persistence,
                repair_context=self.repair_context,
                requests=[req_primary, req_fallback],
                notifications=self.notifications,
            )

        assert summary is None
        assert self.openrouter.chat.await_count == 4
        # Each LLM call (initial + repair) is now persisted with its own
        # attempt_trigger row, so primary+repair+fallback+repair == 4.
        assert self.insert_llm_call_mock.await_count == 4
        fail_mock.assert_awaited_once()
        failed_attempts = fail_mock.await_args.args[5]
        assert len(failed_attempts) == 2
        models_tried = [cfg.model_override or llm.model for llm, cfg in failed_attempts]
        assert models_tried == ["primary", "fallback"]
        self.llm_error_mock.assert_awaited_once()
        _llm_arg, details = self.llm_error_mock.await_args.args
        assert "summary_fields_empty" in (details or "")

    async def test_evaluate_attempt_outcome_exception_still_counts_attempt(
        self,
    ) -> None:
        llm_response = self._llm_response({})
        self.openrouter.chat = AsyncMock(return_value=llm_response)

        with (
            unittest.mock.patch.object(
                self.workflow,
                "_evaluate_attempt_outcome",
                new_callable=AsyncMock,
                side_effect=ValueError("boom"),
            ),
            unittest.mock.patch.object(
                self.workflow,
                "_handle_all_attempts_failed",
                wraps=self.workflow._handle_all_attempts_failed,
                new_callable=AsyncMock,
            ) as fail_mock,
        ):
            summary = await self.workflow.execute_summary_workflow(
                message=MagicMock(),
                req_id=505,
                correlation_id="exception",
                interaction_config=self.interaction,
                persistence=self.persistence,
                repair_context=self.repair_context,
                requests=[self.request],
                notifications=self.notifications,
            )

        assert summary is None
        fail_mock.assert_awaited_once()
        failed_attempts = fail_mock.await_args.args[5]
        assert len(failed_attempts) == 1
        llm_logged, cfg_logged = failed_attempts[0]
        assert llm_logged.error_text == "summary_processing_exception"
        assert cfg_logged.preset_name == self.request.preset_name
        self.llm_error_mock.assert_awaited_once()

    def _llm_response(
        self,
        payload: dict[str, str],
        *,
        status: str = "ok",
        error_text: str | None = None,
        text: str | None = None,
    ) -> SimpleNamespace:
        response_text = text or self._to_json(payload)
        return SimpleNamespace(
            status=status,
            response_json=payload,
            response_text=response_text,
            model="test-model",
            endpoint="/chat",
            request_headers={},
            request_messages=self.base_messages,
            tokens_prompt=50,
            tokens_completion=25,
            cost_usd=0.01,
            latency_ms=120,
            error_text=error_text,
            structured_output_used=True,
            structured_output_mode="json_object",
            error_context=None,
        )

    async def test_llm_call_timeout_fires_independently(self) -> None:
        """LLM call timeout fires even when semaphore is acquired quickly."""
        self.cfg.runtime.llm_call_timeout_sec = 0.05  # 50ms
        self.cfg.runtime.llm_per_model_timeout_min_sec = (
            0.01  # floor below budget so effective_llm_timeout stays at 50ms
        )
        self.cfg.runtime.semaphore_acquire_timeout_sec = 30.0
        self.cfg.runtime.llm_call_max_retries = 0  # No retries for this test

        # Recreate workflow with updated config
        self.workflow = LLMResponseWorkflow(
            cfg=self.cfg,
            db=self.db,
            llm_client=self.openrouter,
            response_formatter=self.response_formatter,
            audit_func=lambda *args, **kwargs: None,
            sem=lambda: _DummySemaphore(),
            **_workflow_repo_kwargs(),
        )

        async def slow_chat(*args, **kwargs):
            await asyncio.sleep(1.0)

        self.openrouter.chat = AsyncMock(side_effect=slow_chat)

        with self.assertRaises(TimeoutError):
            await self.workflow._invoke_llm(self.request, req_id=901)

    async def test_semaphore_timeout_fires_when_semaphore_blocked(self) -> None:
        """Semaphore timeout fires when the semaphore cannot be acquired in time."""
        self.cfg.runtime.semaphore_acquire_timeout_sec = 0.05  # 50ms
        self.cfg.runtime.llm_call_timeout_sec = 180.0

        class _BlockingSemaphore:
            async def __aenter__(self):
                await asyncio.sleep(1.0)

            async def __aexit__(self, *args):
                return None

        workflow = LLMResponseWorkflow(
            cfg=self.cfg,
            db=self.db,
            llm_client=self.openrouter,
            response_formatter=self.response_formatter,
            audit_func=lambda *args, **kwargs: None,
            sem=lambda: _BlockingSemaphore(),
            **_workflow_repo_kwargs(),
        )

        self.openrouter.chat = AsyncMock(return_value=self._llm_response({"tldr": "ok"}))

        with self.assertRaises(TimeoutError):
            await workflow._invoke_llm(self.request, req_id=902)

    async def test_llm_call_timeout_does_not_retry_inside_invoke_llm(self) -> None:
        """After commit 9f50d557 removed the outer retry loop in _invoke_llm,
        a timeout must propagate immediately -- per-model fallback iteration is
        now handled inside OpenRouterChatEngine.chat() via per_model_timeout_sec.
        The on_retry callback on _invoke_llm is dead plumbing and should not fire.
        """
        self.cfg.runtime.llm_call_timeout_sec = 0.05  # 50ms
        self.cfg.runtime.llm_per_model_timeout_min_sec = (
            0.01  # floor below budget so effective_llm_timeout stays at 50ms
        )
        self.cfg.runtime.llm_call_max_retries = 2

        self.workflow = LLMResponseWorkflow(
            cfg=self.cfg,
            db=self.db,
            llm_client=self.openrouter,
            response_formatter=self.response_formatter,
            audit_func=lambda *args, **kwargs: None,
            sem=lambda: _DummySemaphore(),
            **_workflow_repo_kwargs(),
        )

        retry_callback = AsyncMock()

        async def slow_chat(*args, **kwargs):
            await asyncio.sleep(0.1)  # Longer than timeout

        self.openrouter.chat = AsyncMock(side_effect=slow_chat)

        with self.assertRaises(TimeoutError):
            await self.workflow._invoke_llm(self.request, req_id=903, on_retry=retry_callback)

        assert retry_callback.await_count == 0

    async def test_timeout_on_first_attempt_tries_next(self) -> None:
        """TimeoutError on first attempt must not escape; second attempt should succeed.

        llm_call_max_retries=0 ensures _invoke_llm raises on the first timeout
        without internal retries, so the outer except branch is exercised.
        """
        self.cfg.runtime.llm_call_max_retries = 0

        summary_payload = {"summary_250": "From fallback", "tldr": "Fallback TLDR"}
        llm_response = self._llm_response(summary_payload)

        self.openrouter.chat = AsyncMock(side_effect=[TimeoutError("LLM timeout"), llm_response])

        req_primary = LLMRequestConfig(
            messages=self.base_messages,
            response_format={"type": "json_object"},
            max_tokens=256,
            temperature=0.1,
            top_p=1.0,
            model_override="primary-model",
        )
        req_fallback = LLMRequestConfig(
            messages=self.base_messages,
            response_format={"type": "json_object"},
            max_tokens=256,
            temperature=0.1,
            top_p=1.0,
            model_override="fallback-model",
        )

        with unittest.mock.patch(
            "app.adapters.content.llm_response_workflow.parse_summary_response",
            return_value=SimpleNamespace(
                shaped=summary_payload,
                errors=[],
                used_local_fix=False,
            ),
        ):
            summary = await self.workflow.execute_summary_workflow(
                message=MagicMock(),
                req_id=601,
                correlation_id="timeout-fallback",
                interaction_config=self.interaction,
                persistence=self.persistence,
                repair_context=self.repair_context,
                requests=[req_primary, req_fallback],
                notifications=self.notifications,
            )

        assert summary is not None
        assert self.openrouter.chat.await_count == 2
        assert self.insert_llm_call_mock.await_count == 2

    async def test_timeout_on_all_attempts_updates_status(self) -> None:
        """TimeoutError on every attempt must call _handle_all_attempts_failed.

        llm_call_max_retries=0 ensures _invoke_llm raises on the first timeout
        so the outer loop catches it and continues to _handle_all_attempts_failed.
        """
        self.cfg.runtime.llm_call_max_retries = 0
        self.openrouter.chat = AsyncMock(side_effect=TimeoutError("LLM timeout"))

        summary = await self.workflow.execute_summary_workflow(
            message=MagicMock(),
            req_id=602,
            correlation_id="timeout-all",
            interaction_config=self.interaction,
            persistence=self.persistence,
            repair_context=self.repair_context,
            requests=[self.request],
            notifications=self.notifications,
        )

        assert summary is None
        self.update_status_mock.assert_awaited_with(602, "error")

    @staticmethod
    def _to_json(payload: dict[str, str]) -> str:
        items = ", ".join(f'"{k}": "{v}"' for k, v in payload.items())
        return "{" + items + "}"
