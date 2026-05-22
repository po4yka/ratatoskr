import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.adapters.telegram.forward_summarizer import ForwardSummarizer


def _workflow_repo_kwargs() -> dict[str, MagicMock]:
    return {
        "summary_repo": MagicMock(),
        "request_repo": MagicMock(),
        "llm_repo": MagicMock(),
        "user_repo": MagicMock(),
    }


class ForwardSummarizerTests(unittest.IsolatedAsyncioTestCase):
    class _Sem:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    async def test_summarize_forward_delegates_to_workflow(self) -> None:
        cfg = MagicMock()
        cfg.openrouter.temperature = 0.2
        cfg.openrouter.top_p = 1.0
        cfg.openrouter.model = "primary"
        cfg.openrouter.fallback_models = ()
        cfg.openrouter.structured_output_mode = "json_object"
        cfg.runtime.llm_provider = "openrouter"

        db = MagicMock()
        openrouter = MagicMock()
        response_formatter = MagicMock()
        response_formatter.send_forward_completion_notification = AsyncMock()
        response_formatter.send_error_notification = AsyncMock()

        summarizer = ForwardSummarizer(
            cfg,
            db,
            openrouter,
            response_formatter,
            lambda *a, **k: None,
            lambda: self._Sem(),
            **_workflow_repo_kwargs(),
        )

        mock_workflow = AsyncMock(return_value={"summary_250": "ok", "tldr": "fine"})

        prompt = "Forward message"
        message = MagicMock()

        with patch.object(
            summarizer._workflow,
            "execute_summary_workflow",
            new=mock_workflow,
        ):
            result = await summarizer.summarize_forward(
                message=message,
                prompt=prompt,
                chosen_lang="en",
                system_prompt="sys",
                req_id=42,
                correlation_id="cid",
                interaction_id=99,
            )

        assert result == {"summary_250": "ok", "tldr": "fine"}
        mock_workflow.assert_awaited_once()

        call_kwargs = mock_workflow.call_args.kwargs
        requests = call_kwargs["requests"]
        assert len(requests) == 1
        # The full structured summary JSON schema needs at least ~6k output
        # tokens; a smaller cap causes the LLM to truncate, which then trips
        # the budget-tight guard and exhausts the fallback cascade.
        assert requests[0].max_tokens >= 6144

        repair_context = call_kwargs["repair_context"]
        assert repair_context.repair_max_tokens >= 6144
        assert "<untrusted_source_content>" in repair_context.base_messages[1]["content"]
        assert prompt in repair_context.base_messages[1]["content"]

        ensured = await call_kwargs["ensure_summary"]({"summary_250": "ok", "tldr": "fine"})
        assert ensured["summary_quality"]["source_coverage"] == "full"

        notifications = call_kwargs["notifications"]
        assert notifications.completion is not None
        assert notifications.llm_error is not None

    async def test_short_forward_prompt_gets_full_max_tokens_budget(self) -> None:
        """Regression: a SHORT forward must still get >=6144 output tokens.

        The old formula ``max(2048, min(6144, len(prompt)//4 + 2048))`` gave
        only ~2.7k tokens for a ~2.6 KB forward (the "The Bell Tech" case),
        so the LLM truncated mid-JSON, the budget-tight guard skipped
        recovery, and every fallback model died the same way -- user saw
        ``truncation_recovery_skipped_budget_tight`` and "AI Analysis Failed".
        ``max_tokens`` is the OUTPUT budget, not total -- it must not shrink
        with the prompt.
        """
        cfg = MagicMock()
        cfg.openrouter.temperature = 0.2
        cfg.openrouter.top_p = 1.0
        cfg.openrouter.model = "primary"
        cfg.openrouter.fallback_models = ()
        cfg.openrouter.structured_output_mode = "json_object"
        cfg.runtime.llm_provider = "openrouter"

        summarizer = ForwardSummarizer(
            cfg,
            MagicMock(),
            MagicMock(),
            MagicMock(send_forward_completion_notification=AsyncMock()),
            lambda *a, **k: None,
            lambda: self._Sem(),
            **_workflow_repo_kwargs(),
        )

        mock_workflow = AsyncMock(return_value={"summary_250": "ok"})
        short_prompt = "Channel: The Bell Tech\n\n" + ("ru text " * 200)  # ~1.8 KB
        assert len(short_prompt) < 3000  # sanity: the failing-case shape

        with patch.object(
            summarizer._workflow,
            "execute_summary_workflow",
            new=mock_workflow,
        ):
            await summarizer.summarize_forward(
                message=MagicMock(),
                prompt=short_prompt,
                chosen_lang="ru",
                system_prompt="sys",
                req_id=1,
                correlation_id="cid",
                interaction_id=1,
            )

        requests = mock_workflow.call_args.kwargs["requests"]
        assert requests[0].max_tokens >= 6144, (
            f"short forward got {requests[0].max_tokens} output tokens -- "
            "too few for the structured summary JSON schema, will truncate"
        )
        repair_context = mock_workflow.call_args.kwargs["repair_context"]
        assert repair_context.repair_max_tokens >= 6144
