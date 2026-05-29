import unittest
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from app.adapters.content.interactive_summary_service import InteractiveSummaryService
from app.adapters.content.pure_summary_service import PureSummaryService
from app.adapters.content.summarization_models import InteractiveSummaryRequest
from app.adapters.content.summarization_runtime import SummarizationRuntime
from app.adapters.content.summary_request_factory import SummaryRequestFactory

if TYPE_CHECKING:
    from app.config import AppConfig


class _DummySemaphore:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _runtime_repo_kwargs() -> dict[str, Any]:
    return {
        "summary_repo": MagicMock(),
        "request_repo": MagicMock(),
        "crawl_result_repo": MagicMock(),
        "llm_repo": MagicMock(),
        "user_repo": MagicMock(),
    }


class InteractiveSummaryServiceRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cfg = SimpleNamespace(
            openrouter=SimpleNamespace(
                model="primary-model",
                fallback_models=("fallback-model",),
                flash_model=None,
                flash_fallback_models=(),
                temperature=0.2,
                top_p=0.9,
                max_tokens=None,
                long_context_model=None,
                summary_temperature_relaxed=None,
                summary_top_p_relaxed=None,
                summary_temperature_json_fallback=None,
                summary_top_p_json_fallback=None,
                enable_structured_outputs=True,
                structured_output_mode="json_schema",
                require_parameters=True,
                auto_fallback_structured=True,
            ),
            runtime=SimpleNamespace(
                summary_prompt_version="v1",
                summary_two_pass_enabled=False,
            ),
            web_search=SimpleNamespace(enabled=False),
            redis=SimpleNamespace(
                enabled=False,
                cache_enabled=False,
                prefix="",
                required=False,
                cache_timeout_sec=0.3,
                llm_ttl_seconds=7_200,
            ),
            attachment=SimpleNamespace(vision_model="vision-model"),
            model_routing=SimpleNamespace(enabled=False, fallback_models=()),
        )

        self.db = MagicMock()
        self.response_formatter = MagicMock()
        self.response_formatter.send_llm_completion_notification = AsyncMock()
        self.response_formatter.send_llm_start_notification = AsyncMock()
        self.response_formatter.send_error_notification = AsyncMock()
        self.response_formatter.send_cached_summary_notification = AsyncMock()
        self.openrouter = MagicMock()

    async def test_builds_parameter_presets_and_fallbacks(self) -> None:
        cache_stub = MagicMock()
        cache_stub.enabled = False
        cache_stub.get_json = AsyncMock(return_value=None)
        cache_stub.set_json = AsyncMock()

        runtime = SummarizationRuntime(
            cfg=cast("AppConfig", self.cfg),
            db=self.db,
            openrouter=self.openrouter,
            response_formatter=self.response_formatter,
            audit_func=lambda *args, **kwargs: None,
            sem=lambda: _DummySemaphore(),
            cache=cache_stub,
            **_runtime_repo_kwargs(),
        )
        pure_service = PureSummaryService(runtime=runtime)
        request_factory = SummaryRequestFactory(
            runtime=runtime,
            select_max_tokens=pure_service.select_max_tokens,
        )
        service = InteractiveSummaryService(
            runtime=runtime,
            request_factory=request_factory,
            pure_summary_service=pure_service,
        )

        captured_requests: list[Any] = []

        async def _capture_requests(**kwargs: Any):
            captured_requests.extend(kwargs.get("requests") or [])
            return {"summary_250": "ok", "summary_1000": "ok", "tldr": "ok"}

        with (
            patch.object(
                runtime.workflow,
                "execute_summary_workflow",
                AsyncMock(side_effect=_capture_requests),
            ),
            patch.object(
                runtime.metadata_helper,
                "ensure_summary_metadata",
                AsyncMock(side_effect=lambda summary, *args, **kwargs: summary),
            ),
        ):
            result = await service.summarize(
                InteractiveSummaryRequest(
                    message=MagicMock(),
                    content_text="short content for testing.",
                    chosen_lang="en",
                    system_prompt="system prompt",
                    req_id=1,
                    max_chars=10_000,
                    correlation_id="cid",
                )
            )

        assert result.summary is not None
        assert captured_requests, "requests should be built and passed to the workflow"
        preset_names = [req.preset_name for req in captured_requests]
        assert preset_names == [
            "schema_strict",
            "json_object_guardrail",
            "json_object_fallback",
        ]
        assert captured_requests[0].model_override == "primary-model"
        assert captured_requests[-1].model_override == "fallback-model"
        assert captured_requests[2].response_format.get("type") == "json_object"
        assert captured_requests[0].response_format.get("type") == "json_schema"
