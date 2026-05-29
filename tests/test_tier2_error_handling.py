"""Tests for Tier 2 CancelledError propagation in error handlers.

Verifies that ``except Exception`` blocks in search_handler, decorators,
url_processor, and llm_response_workflow correctly re-raise asyncio.CancelledError
instead of silently swallowing it, and that non-cancellation exceptions produce
debug logging rather than being silently suppressed.

Note: On Python 3.9+ asyncio.CancelledError is a BaseException (not caught by
``except Exception``). The raise_if_cancelled guards are defensive: they document
intent and protect against future regressions or wrapped cancellation errors.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.services.summarization.llm_response_workflow import LLMResponseWorkflow

# ---------------------------------------------------------------------------
# LLMResponseWorkflow._set_failure_context
# ---------------------------------------------------------------------------


class TestSetFailureContextCancelledError:
    """_set_failure_context must propagate CancelledError from attribute access."""

    def _make_workflow(self) -> LLMResponseWorkflow:
        """Create a minimal LLMResponseWorkflow for testing _set_failure_context."""
        return LLMResponseWorkflow(
            cfg=MagicMock(),
            db=MagicMock(),
            openrouter=MagicMock(),
            response_formatter=MagicMock(),
            audit_func=MagicMock(),
            sem=MagicMock(),
            summary_repo=MagicMock(),
            request_repo=MagicMock(),
            llm_repo=MagicMock(),
            user_repo=MagicMock(),
        )

    def test_propagates_cancelled_on_error_text(self) -> None:
        """CancelledError raised during error_text assignment must propagate."""
        wf = self._make_workflow()

        class CancellingLLM:
            @property
            def error_text(self) -> str | None:
                return None

            @error_text.setter
            def error_text(self, _value: str) -> None:
                raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            wf._set_failure_context(CancellingLLM(), "test_reason")

    def test_propagates_cancelled_on_error_context(self) -> None:
        """CancelledError raised during error_context assignment must propagate."""
        wf = self._make_workflow()

        class CancellingLLM:
            error_text: str | None = None

            @property
            def error_context(self) -> dict[str, Any] | None:
                return None

            @error_context.setter
            def error_context(self, _value: dict[str, Any]) -> None:
                raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            wf._set_failure_context(CancellingLLM(), "test_reason")

    def test_propagates_regular_exception(self) -> None:
        """Non-CancelledError exceptions from attribute access propagate (no try/except)."""
        wf = self._make_workflow()

        class BrokenLLM:
            @property
            def error_text(self) -> str | None:
                return None

            @error_text.setter
            def error_text(self, _value: str) -> None:
                raise TypeError("immutable")

            @property
            def error_context(self) -> dict[str, Any] | None:
                return None

            @error_context.setter
            def error_context(self, _value: dict[str, Any]) -> None:
                raise TypeError("immutable")

        # _set_failure_context does not catch exceptions from attribute access
        with pytest.raises(TypeError, match="immutable"):
            wf._set_failure_context(BrokenLLM(), "test_reason")


# ---------------------------------------------------------------------------
# execute_summary_workflow -- error_context setting in main loop
# ---------------------------------------------------------------------------


class TestSummaryWorkflowLoopCancelledError:
    """The error_context block inside execute_summary_workflow must propagate CancelledError."""

    @pytest.mark.asyncio
    async def test_loop_error_context_propagates_cancelled(self) -> None:
        """When setting error_context raises CancelledError it must not be swallowed."""
        from app.application.services.summarization.llm_response_workflow import (
            LLMInteractionConfig,
            LLMRepairContext,
            LLMRequestConfig,
            LLMSummaryPersistenceSettings,
        )

        sem_ctx = MagicMock()
        sem_ctx.__aenter__ = AsyncMock(return_value=None)
        sem_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_cfg = MagicMock(
            openrouter=MagicMock(model="test", structured_output_mode="json_object")
        )
        mock_cfg.runtime.semaphore_acquire_timeout_sec = 30.0
        mock_cfg.runtime.llm_call_timeout_sec = 180.0
        mock_cfg.runtime.json_parse_timeout_sec = 60.0

        wf = LLMResponseWorkflow(
            cfg=mock_cfg,
            db=MagicMock(),
            openrouter=AsyncMock(),
            response_formatter=MagicMock(),
            audit_func=MagicMock(),
            sem=MagicMock(return_value=sem_ctx),
            summary_repo=MagicMock(),
            request_repo=MagicMock(),
            llm_repo=MagicMock(),
            user_repo=MagicMock(),
        )

        class CancellingLLM:
            status = "ok"
            response_json: dict[str, Any] | None = None
            response_text = "{}"
            error_text: str | None = None
            model = "test"
            endpoint = "test"
            tokens_prompt = 0
            tokens_completion = 0
            cost_usd = 0.0
            latency_ms = 100

            def __init__(self) -> None:
                self.request_headers: dict[str, Any] = {}
                self.request_messages: list[Any] = []

            @property
            def error_context(self) -> dict[str, Any] | None:
                return None

            @error_context.setter
            def error_context(self, _value: dict[str, Any]) -> None:
                raise asyncio.CancelledError()

        wf.openrouter.chat = AsyncMock(return_value=CancellingLLM())

        with patch.object(wf, "_evaluate_attempt_outcome", side_effect=RuntimeError("boom")):
            with patch.object(wf, "_persist_llm_call", new_callable=AsyncMock):
                with pytest.raises(asyncio.CancelledError):
                    await wf.execute_summary_workflow(
                        message=MagicMock(),
                        req_id=1,
                        correlation_id="test-cid",
                        interaction_config=LLMInteractionConfig(),
                        persistence=LLMSummaryPersistenceSettings(lang="en"),
                        repair_context=LLMRepairContext(
                            base_messages=[],
                            repair_response_format={"type": "json_object"},
                            default_prompt="fix it",
                        ),
                        requests=[
                            LLMRequestConfig(
                                messages=[{"role": "user", "content": "test"}],
                                response_format={"type": "json_object"},
                            ),
                        ],
                    )


# ---------------------------------------------------------------------------
# decorators.audit_command
# ---------------------------------------------------------------------------


class TestAuditCommandCancelledError:
    """audit_command decorator must propagate CancelledError from audit_func."""

    @pytest.mark.asyncio
    async def test_propagates_cancelled(self) -> None:
        from app.adapters.telegram.command_handlers.decorators import audit_command

        @audit_command("test_event")
        async def handler(self: Any, ctx: Any) -> str:
            return "ok"

        ctx = SimpleNamespace(
            uid=1,
            chat_id=1,
            correlation_id="test",
            text="hello world",
            audit_func=MagicMock(side_effect=asyncio.CancelledError()),
        )

        with pytest.raises(asyncio.CancelledError):
            await handler(None, ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_swallows_regular_exception_and_logs(self) -> None:
        from unittest.mock import patch

        from app.adapters.telegram.command_handlers.decorators import audit_command

        @audit_command("test_event")
        async def handler(self: Any, ctx: Any) -> str:
            return "ok"

        ctx = SimpleNamespace(
            uid=1,
            chat_id=1,
            correlation_id="test",
            text="hello world",
            audit_func=MagicMock(side_effect=RuntimeError("audit broken")),
        )

        with patch("app.adapters.telegram.command_handlers.decorators.logger") as mock_logger:
            result = await handler(None, ctx)  # type: ignore[arg-type]

        assert result == "ok"
        assert any("audit_log_failed" in str(call) for call in mock_logger.warning.call_args_list)


# ---------------------------------------------------------------------------
# search_handler audit logging
# ---------------------------------------------------------------------------


class TestSearchHandlerAuditCancelledError:
    """Search handler audit block must propagate CancelledError."""

    @pytest.mark.asyncio
    async def test_propagates_cancelled(self) -> None:
        from app.adapters.telegram.command_handlers.search_handler import (
            SearchHandler,
        )

        sh = SearchHandler(
            response_formatter=MagicMock(),
            searcher_provider=SimpleNamespace(
                topic_searcher=None,
                local_searcher=None,
                hybrid_search=None,
            ),
        )

        ctx = SimpleNamespace(
            uid=1,
            chat_id=1,
            correlation_id="test",
            text="/find test topic",
            message=MagicMock(),
            interaction_id=None,
            start_time=0,
            user_repo=MagicMock(),
            audit_func=MagicMock(side_effect=asyncio.CancelledError()),
        )

        with pytest.raises(asyncio.CancelledError):
            await sh._handle_topic_search(
                ctx,  # type: ignore[arg-type]
                command="/find",
                searcher=None,
                unavailable_message="unavail",
                usage_example="usage {cmd}",
                invalid_message="invalid {cmd}",
                error_message="error",
                empty_message="empty {topic}",
                response_prefix="test",
                log_event="test_event",
                formatter_source="test",
            )

    @pytest.mark.asyncio
    async def test_swallows_regular_exception_and_logs(self) -> None:
        from unittest.mock import patch

        from app.adapters.telegram.command_handlers.search_handler import (
            SearchHandler,
        )

        formatter = MagicMock()
        formatter.safe_reply = AsyncMock()

        sh = SearchHandler(
            response_formatter=formatter,
            searcher_provider=SimpleNamespace(
                topic_searcher=None,
                local_searcher=None,
                hybrid_search=None,
            ),
        )

        ctx = SimpleNamespace(
            uid=1,
            chat_id=1,
            correlation_id="test",
            text="/find test topic",
            message=MagicMock(),
            interaction_id=None,
            start_time=0,
            user_repo=MagicMock(),
            audit_func=MagicMock(side_effect=RuntimeError("audit broken")),
        )

        with patch("app.adapters.telegram.command_handlers.search_handler.logger") as mock_logger:
            await sh._handle_topic_search(
                ctx,  # type: ignore[arg-type]
                command="/find",
                searcher=None,
                unavailable_message="unavail",
                usage_example="usage {cmd}",
                invalid_message="invalid {cmd}",
                error_message="error",
                empty_message="empty {topic}",
                response_prefix="test",
                log_event="test_event",
                formatter_source="test",
            )

        assert any("audit_log_failed" in str(call) for call in mock_logger.warning.call_args_list)


# ---------------------------------------------------------------------------
# url_post_summary_task_service -- Russian translation fallback
# ---------------------------------------------------------------------------


class TestURLPostSummaryTaskServiceRuTranslationCancelledError:
    """Russian translation error reply must propagate CancelledError."""

    @pytest.mark.asyncio
    async def test_propagates_cancelled(self) -> None:
        from app.adapters.content.url_post_summary_task_service import URLPostSummaryTaskService

        formatter = MagicMock()
        formatter.safe_reply = AsyncMock(side_effect=asyncio.CancelledError())
        service = URLPostSummaryTaskService(
            response_formatter=formatter,
            summary_repo=MagicMock(),
            article_generator=MagicMock(),
            insights_generator=MagicMock(),
            summary_delivery=MagicMock(),
        )

        service.translate_summary_to_ru = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("translate failed")
        )

        with pytest.raises(asyncio.CancelledError):
            await service._maybe_send_russian_translation(
                message=MagicMock(),
                summary={"tldr": "test"},
                req_id=1,
                correlation_id="test-cid",
                needs_translation=True,
            )
