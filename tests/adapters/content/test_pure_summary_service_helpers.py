from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.content.pure_summary_service import PureSummaryService
from app.core.call_status import CallStatus
from app.core.lang import LANG_RU


@asynccontextmanager
async def _sem() -> Any:
    yield


class _OpenRouter:
    def __init__(self, result: object | None = None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> object:
        self.calls.append({"messages": messages, **kwargs})
        if self.exc:
            raise self.exc
        return self.result


class _Workflow:
    def build_structured_response_format(self, *, mode: str) -> dict[str, str]:
        return {"mode": mode}


def _service(openrouter: _OpenRouter | None = None) -> PureSummaryService:
    cfg = SimpleNamespace(
        openrouter=SimpleNamespace(
            max_tokens=None,
            temperature=0.2,
            top_p=0.9,
            model="model",
            long_context_model=None,
            structured_output_mode="json",
        ),
        model_routing=SimpleNamespace(
            enabled=False, long_context_threshold_tokens=100, long_context_model=None
        ),
        runtime=SimpleNamespace(summarization_max_retries=1),
    )
    runtime = SimpleNamespace(
        cfg=cfg,
        sem=lambda: _sem(),
        openrouter=openrouter or _OpenRouter(),
        workflow=_Workflow(),
    )
    return PureSummaryService(runtime=runtime)  # type: ignore[arg-type]


def test_parse_summary_from_llm_result_handles_json_shapes() -> None:
    service = _service()

    assert service.parse_summary_from_llm_result(
        SimpleNamespace(
            response_json={"choices": [{"message": {"parsed": {"a": 1}}}]}, response_text=None
        )
    ) == {"a": 1}
    assert service.parse_summary_from_llm_result(
        SimpleNamespace(
            response_json={"choices": [{"message": {"content": 'prefix {"b":2}'}}]},
            response_text=None,
        )
    ) == {"b": 2}
    assert service.parse_summary_from_llm_result(
        SimpleNamespace(response_json=None, response_text='prefix {"c":3}')
    ) == {"c": 3}
    assert (
        service.parse_summary_from_llm_result(
            SimpleNamespace(response_json={"choices": []}, response_text=None)
        )
        is None
    )


def test_select_max_tokens_uses_dynamic_and_configured_limits() -> None:
    service = _service()
    assert service.select_max_tokens("short text") == 4096

    service._runtime.cfg.openrouter.max_tokens = 5000
    assert service.select_max_tokens("short text") == 4096

    long_text = "word " * 30000
    selected = service.select_max_tokens(long_text)
    assert 4096 <= selected <= 5000


def test_quality_metadata_and_truncate(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    summary: dict[str, Any] = {}

    result = service._apply_request_quality_metadata(
        summary,
        source_coverage="full",
        extraction_quality="high",
        extraction_confidence=0.9,
        prompt_injection_suspected=True,
    )

    assert result is summary
    assert summary["summary_quality"]["source_coverage"] == "full"
    assert summary["summary_quality"]["prompt_injection_suspected"] is True

    monkeypatch.setattr(
        "app.adapters.content.llm_summarizer_text.truncate_content_text",
        lambda text, max_chars: text[:max_chars],
    )
    assert service._truncate_content("abcdef", 3) == "abc"


@pytest.mark.asyncio
async def test_enrich_two_pass_merges_selected_fields() -> None:
    llm_result = SimpleNamespace(
        status=CallStatus.OK,
        error_text=None,
        response_json={
            "choices": [
                {
                    "message": {
                        "parsed": {
                            "seo_keywords": ["kw"],
                            "highlights": ["h"],
                            "ignored": True,
                        }
                    }
                }
            ]
        },
        response_text=None,
    )
    openrouter = _OpenRouter(llm_result)
    service = _service(openrouter)
    summary = {"summary_250": "s", "topic_tags": ["t"]}

    result = await service.enrich_two_pass(
        summary,
        content_text="content",
        chosen_lang=LANG_RU,
        correlation_id="cid",
    )

    assert result["seo_keywords"] == ["kw"]
    assert result["highlights"] == ["h"]
    assert "ignored" not in result
    assert openrouter.calls[0]["request_id"] is None


@pytest.mark.asyncio
async def test_enrich_two_pass_returns_original_on_failure() -> None:
    summary = {"summary_250": "s"}
    failed = SimpleNamespace(
        status=CallStatus.ERROR, error_text="bad", response_json=None, response_text=None
    )
    assert (
        await _service(_OpenRouter(failed)).enrich_two_pass(
            summary,
            content_text="content",
            chosen_lang="en",
            correlation_id="cid",
        )
        is summary
    )
    assert (
        await _service(_OpenRouter(exc=RuntimeError("down"))).enrich_two_pass(
            summary,
            content_text="content",
            chosen_lang="en",
            correlation_id="cid",
        )
        is summary
    )
