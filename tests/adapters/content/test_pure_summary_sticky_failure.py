"""Unit tests for the sticky-failure short-circuit in PureSummaryService.

Covers four scenarios:
1. Sticky error on attempt 0 -> override dropped -> second attempt succeeds.
2. Flag disabled -> first failure propagates immediately (no retry).
3. Non-sticky error -> propagates without retry.
4. Both attempts fail -> second exception propagates.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.content.pure_summary_service import PureSummaryService


@asynccontextmanager
async def _sem() -> Any:
    yield


def _make_structured_result(model_used: str = "fallback-model") -> Any:
    """Minimal StructuredLLMResult-like object returned by chat_structured."""
    parsed = MagicMock()
    parsed.model_dump.return_value = {"summary_250": "ok", "quality": {}}
    return SimpleNamespace(
        parsed=parsed,
        model_used=model_used,
        tokens_prompt=10,
        tokens_completion=20,
        latency_ms=500,
    )


def _make_service(
    *,
    call_results: list[Any],
    sticky_fallback_enabled: bool = True,
    model_override_in_cfg: str | None = None,
) -> tuple[PureSummaryService, list[dict[str, Any]]]:
    """Build a PureSummaryService whose openrouter.chat_structured plays back *call_results*.

    Each element of *call_results* is either an Exception (to raise) or a value
    (to return).  A parallel list records call kwargs for assertion.
    """
    call_log: list[dict[str, Any]] = []
    call_iter = iter(call_results)

    async def _chat_structured(messages: Any, **kwargs: Any) -> Any:
        call_log.append({"messages": messages, **kwargs})
        nxt = next(call_iter)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    openrouter = SimpleNamespace(chat_structured=_chat_structured)

    cfg = SimpleNamespace(
        openrouter=SimpleNamespace(
            max_tokens=None,
            temperature=0.2,
            top_p=0.9,
            model=model_override_in_cfg or "default-model",
            long_context_model=None,
            structured_output_mode="json",
        ),
        model_routing=SimpleNamespace(
            enabled=False,
            long_context_threshold_tokens=100,
            long_context_model=None,
        ),
        runtime=SimpleNamespace(
            summarization_max_retries=1,
            llm_sticky_failure_force_fallback=sticky_fallback_enabled,
        ),
    )

    runtime = SimpleNamespace(
        cfg=cfg,
        sem=lambda: _sem(),
        openrouter=openrouter,
    )

    # Patch out post-processing helpers so we don't need a full summary schema.
    import app.adapters.content.pure_summary_service as _mod

    original_mark = _mod.mark_prompt_injection_metadata
    original_merge = _mod.merge_summary_quality_metadata

    def _noop_mark(summary: dict[str, Any], _content: str) -> dict[str, Any]:
        return summary

    def _noop_merge(summary: dict[str, Any], **_kwargs: Any) -> None:
        pass

    _mod.mark_prompt_injection_metadata = _noop_mark  # type: ignore[assignment]
    _mod.merge_summary_quality_metadata = _noop_merge  # type: ignore[assignment]

    service = PureSummaryService(runtime=runtime)  # type: ignore[arg-type]

    # Restore originals after service is constructed; tests run immediately.
    _mod.mark_prompt_injection_metadata = original_mark  # type: ignore[assignment]
    _mod.merge_summary_quality_metadata = original_merge  # type: ignore[assignment]

    # Patch at the module level for the duration of each test via monkeypatch
    # would be cleaner, but keeping it self-contained here is simpler for this
    # targeted unit.  We patch the module-level names before each async call
    # instead.
    service._noop_mark = _noop_mark  # type: ignore[attr-defined]
    service._noop_merge = _noop_merge  # type: ignore[attr-defined]

    return service, call_log


async def _call_summarize(
    service: PureSummaryService,
    model_override: str | None = "primary-model",
) -> dict[str, Any]:
    """Invoke _summarize_with_instructor with helpers patched to no-ops."""
    import app.adapters.content.pure_summary_service as _mod

    original_mark = _mod.mark_prompt_injection_metadata
    original_merge = _mod.merge_summary_quality_metadata

    _mod.mark_prompt_injection_metadata = service._noop_mark  # type: ignore[attr-defined]
    _mod.merge_summary_quality_metadata = service._noop_merge  # type: ignore[attr-defined]

    # Also patch the SummaryModel import inside _summarize_with_instructor.
    import sys

    dummy_model = MagicMock()
    fake_schema_mod = MagicMock()
    fake_schema_mod.SummaryModel = dummy_model
    sys.modules["app.core.summary_schema"] = fake_schema_mod

    try:
        return await service._summarize_with_instructor(
            messages=[{"role": "user", "content": "summarize this"}],
            source_content="test content",
            max_tokens=4096,
            model_override=model_override,
            correlation_id="test-cid",
        )
    finally:
        _mod.mark_prompt_injection_metadata = original_mark
        _mod.merge_summary_quality_metadata = original_merge
        del sys.modules["app.core.summary_schema"]


@pytest.mark.asyncio
async def test_sticky_error_drops_override_and_succeeds_on_retry() -> None:
    """With the flag enabled, a per_model_timeout on attempt 0 triggers a retry
    with model_override=None, and the second call succeeds."""
    sticky_exc = ValueError("per_model_timeout: model primary-model exceeded 144s")
    second_result = _make_structured_result("fallback-model")

    service, call_log = _make_service(call_results=[sticky_exc, second_result])

    result = await _call_summarize(service, model_override="primary-model")

    # Two calls were made.
    assert len(call_log) == 2
    # First call had the override.
    assert call_log[0]["model_override"] == "primary-model"
    # Second call had the override dropped (None).
    assert call_log[1]["model_override"] is None
    # Result came from the second call.
    assert result["summary_250"] == "ok"


@pytest.mark.asyncio
async def test_flag_disabled_propagates_first_failure() -> None:
    """With the flag disabled, the first sticky failure propagates immediately."""
    sticky_exc = ValueError("per_model_timeout: model primary-model exceeded 144s")

    service, call_log = _make_service(
        call_results=[sticky_exc],
        sticky_fallback_enabled=False,
    )

    with pytest.raises(ValueError, match="Instructor LLM call failed"):
        await _call_summarize(service, model_override="primary-model")

    # Only one call was made — no retry.
    assert len(call_log) == 1


@pytest.mark.asyncio
async def test_non_sticky_error_propagates_without_retry() -> None:
    """A non-sticky exception is not retried even when the flag is enabled."""
    non_sticky_exc = ValueError("unrelated network blip")

    service, call_log = _make_service(call_results=[non_sticky_exc])

    with pytest.raises(ValueError, match="Instructor LLM call failed"):
        await _call_summarize(service, model_override="primary-model")

    # Only one call — the non-sticky guard must not retry.
    assert len(call_log) == 1


@pytest.mark.asyncio
async def test_both_attempts_fail_second_exception_propagates() -> None:
    """If the fallback attempt also fails, the second exception propagates."""
    first_exc = ValueError("repeated_truncation on primary-model")
    second_exc = ValueError("second model also failed")

    service, call_log = _make_service(call_results=[first_exc, second_exc])

    with pytest.raises(ValueError, match="Instructor LLM call failed"):
        await _call_summarize(service, model_override="primary-model")

    # Both calls were made.
    assert len(call_log) == 2
    # Second call had no override.
    assert call_log[1]["model_override"] is None
