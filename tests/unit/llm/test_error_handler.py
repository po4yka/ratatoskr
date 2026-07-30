"""Unit tests for ``OpenRouterErrorHandler`` (the retry/fallback policy).

Directly exercises the explicit goal criteria:

- **Provider fallback on 5xx**: ``should_try_next_model``/``should_retry``.
- **Retry-with-backoff bound**: ``should_retry`` returns False once attempt
  reaches ``max_retries``; ``sleep_backoff`` honours the configured base.
- **Timeout handling**: 408/504 trigger model fallback; text-level "timeout"
  in the error blob also fans out.

Every assertion is offline.
"""

from __future__ import annotations

import pytest

from app.adapters.openrouter.error_handler import ErrorHandler

pytestmark = pytest.mark.no_network


def _make_handler(*, max_retries: int = 3, backoff_base: float = 0.01) -> ErrorHandler:
    return ErrorHandler(max_retries=max_retries, backoff_base=backoff_base)


# ---------------------------------------------------------------------------
# should_retry — retry budget
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504, 408])
def test_should_retry_returns_true_for_retryable_status_codes_within_budget(
    status_code: int,
) -> None:
    h = _make_handler(max_retries=3)
    assert h.should_retry(status_code, attempt=0) is True
    assert h.should_retry(status_code, attempt=2) is True


@pytest.mark.parametrize("status_code", [200, 400, 401, 402, 403, 404])
def test_should_retry_returns_false_for_non_retryable_status_codes(status_code: int) -> None:
    h = _make_handler(max_retries=3)
    assert h.should_retry(status_code, attempt=0) is False


def test_should_retry_returns_false_once_attempt_reaches_max_retries() -> None:
    """The retry budget MUST be bounded by ``max_retries``."""
    h = _make_handler(max_retries=2)
    assert h.should_retry(500, attempt=0) is True
    assert h.should_retry(500, attempt=1) is True
    # attempt == max_retries: exhausted.
    assert h.should_retry(500, attempt=2) is False
    assert h.should_retry(500, attempt=99) is False


@pytest.mark.parametrize("status_code", [400, 401, 402, 403])
def test_is_non_retryable_error_for_4xx_client_errors(status_code: int) -> None:
    assert _make_handler().is_non_retryable_error(status_code) is True


@pytest.mark.parametrize("status_code", [429, 500, 502])
def test_is_non_retryable_error_false_for_retryable_5xx_or_429(status_code: int) -> None:
    assert _make_handler().is_non_retryable_error(status_code) is False


# ---------------------------------------------------------------------------
# sleep_backoff — bounded exponential backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sleep_backoff_delegates_to_central_helper_with_configured_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sleep_backoff`` must forward the attempt + base to the shared helper."""
    recorded: dict[str, float] = {}

    async def _fake(attempt: int, base: float) -> None:
        recorded["attempt"] = attempt
        recorded["base"] = base

    monkeypatch.setattr(
        "app.adapters.openrouter.error_handler._sleep_backoff",
        _fake,
    )

    h = _make_handler(backoff_base=0.5)
    await h.sleep_backoff(attempt=3)
    assert recorded == {"attempt": 3, "base": 0.5}


# ---------------------------------------------------------------------------
# should_try_next_model — 5xx / 404 / timeout fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [404, 408, 504])
def test_should_try_next_model_for_404_408_504(status_code: int) -> None:
    assert _make_handler().should_try_next_model(status_code) is True


def test_should_try_next_model_detects_timeout_text() -> None:
    h = _make_handler()
    assert h.should_try_next_model(500, error_text="Request timeout occurred") is True
    assert h.should_try_next_model(500, error_text="Read TIMEOUT") is True
    assert h.should_try_next_model(500, error_text="generic boom") is False


def test_should_try_next_model_false_for_clean_5xx_without_timeout_hint() -> None:
    assert _make_handler().should_try_next_model(502) is False
    assert _make_handler().should_try_next_model(500) is False


def test_should_try_next_model_false_for_2xx_and_4xx_client_errors() -> None:
    h = _make_handler()
    assert h.should_try_next_model(200) is False
    assert h.should_try_next_model(401) is False


# ---------------------------------------------------------------------------
# is_schema_construct_rejection — JSON-schema construct hints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {"error": {"message": "additionalProperties not supported"}},
        {"error": {"message": "oneOf rejected by provider"}},
        {"error": {"message": "anyOf rejected"}},
        {"error": {"message": "Schema with $ref unsupported"}},
        {"error": {"message": "$defs is not allowed here"}},
    ],
)
def test_is_schema_construct_rejection_true_for_known_construct_complaints(
    data: dict,
) -> None:
    assert _make_handler().is_schema_construct_rejection(data) is True


def test_is_schema_construct_rejection_false_for_unrelated_400() -> None:
    assert (
        _make_handler().is_schema_construct_rejection(
            {"error": {"message": "missing required field"}}
        )
        is False
    )


def test_is_schema_construct_rejection_false_for_non_dict_payload() -> None:
    assert _make_handler().is_schema_construct_rejection("oops") is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# is_provider_specific_rejection
# ---------------------------------------------------------------------------


def test_is_provider_specific_rejection_true_when_metadata_has_provider_name() -> None:
    data = {"error": {"metadata": {"provider_name": "Anthropic"}}}
    assert _make_handler().is_provider_specific_rejection(data) is True


def test_is_provider_specific_rejection_false_without_metadata() -> None:
    assert _make_handler().is_provider_specific_rejection({"error": {}}) is False
    assert _make_handler().is_provider_specific_rejection({}) is False


# ---------------------------------------------------------------------------
# should_downgrade_response_format
# ---------------------------------------------------------------------------


def test_downgrade_response_format_from_json_schema_to_json_object() -> None:
    h = _make_handler()
    data = {"error": {"message": "response_format json_schema not supported"}}
    should, new_mode = h.should_downgrade_response_format(
        status_code=400,
        data=data,
        rf_mode_current="json_schema",
        rf_included=True,
        attempt=0,
    )
    assert should is True and new_mode == "json_object"


def test_downgrade_response_format_disables_structured_when_object_also_unsupported() -> None:
    h = _make_handler()
    data = {"error": {"message": "response_format unsupported"}}
    should, new_mode = h.should_downgrade_response_format(
        status_code=400,
        data=data,
        rf_mode_current="json_object",
        rf_included=True,
        attempt=0,
    )
    assert should is True and new_mode is None


def test_downgrade_response_format_inactive_when_disabled() -> None:
    h = ErrorHandler(auto_fallback_structured=False)
    data = {"error": {"message": "response_format unsupported"}}
    should, new_mode = h.should_downgrade_response_format(
        status_code=400,
        data=data,
        rf_mode_current="json_schema",
        rf_included=True,
        attempt=0,
    )
    assert should is False and new_mode is None


def test_downgrade_response_format_inactive_when_rf_not_included() -> None:
    h = _make_handler()
    should, _ = h.should_downgrade_response_format(
        status_code=400,
        data={"error": {"message": "response_format invalid"}},
        rf_mode_current="json_schema",
        rf_included=False,
        attempt=0,
    )
    assert should is False


def test_downgrade_response_format_inactive_for_non_400_status() -> None:
    h = _make_handler()
    should, _ = h.should_downgrade_response_format(
        status_code=500,
        data={"error": {"message": "response_format json_schema bad"}},
        rf_mode_current="json_schema",
        rf_included=True,
        attempt=0,
    )
    assert should is False


# ---------------------------------------------------------------------------
# handle_rate_limit — Retry-After header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_rate_limit_sleeps_for_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []

    async def _fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)
    h = _make_handler()
    # The caller uses the return value to decide whether the standard backoff
    # still has to run, so it is part of the contract, not a detail.
    assert await h.handle_rate_limit({"retry-after": "7"}) is True
    assert slept == [7]


@pytest.mark.asyncio
async def test_handle_rate_limit_swallows_invalid_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(
        "asyncio.sleep",
        lambda s: slept.append(s),  # type: ignore[arg-type, unused-ignore]
    )
    h = _make_handler()
    # Non-numeric retry-after must not crash, and must report that it did not
    # wait -- otherwise chat_response_handler skips its backoff and retries a
    # rate-limited provider immediately.
    assert await h.handle_rate_limit({"retry-after": "soon"}) is False
    assert slept == []  # nothing scheduled


@pytest.mark.asyncio
async def test_handle_rate_limit_no_op_without_header() -> None:
    """Missing retry-after header must not sleep or raise, and reports no wait."""
    h = _make_handler()
    assert await h.handle_rate_limit({}) is False


# ---------------------------------------------------------------------------
# Audit logging — fire-and-forget when callback is configured
# ---------------------------------------------------------------------------


def test_audit_callbacks_route_events_to_provided_function() -> None:
    received: list[tuple[str, str, dict]] = []
    audit = lambda lvl, ev, d: received.append((lvl, ev, d))  # noqa: E731
    h = ErrorHandler(audit=audit)
    h.log_attempt(attempt=1, model="m1", request_id=11)
    h.log_success(
        attempt=1,
        model="m1",
        status_code=200,
        latency=12,
        structured_output_used=True,
        structured_output_mode="json_schema",
    )
    h.log_error(attempt=2, model="m1", status_code=500, error_message="boom")
    h.log_fallback(from_model="m1", to_model="m2")
    h.log_exhausted(models_tried=["m1", "m2"], attempts_each=3, error="boom")
    h.log_skip_model(model="m1", reason="circuit_open")
    h.log_response_format_downgrade(model="m1", from_mode="json_schema", to_mode="json_object")

    events = [event for _, event, _ in received]
    assert "openrouter_attempt" in events
    assert "openrouter_success" in events
    assert "openrouter_error" in events
    assert "openrouter_fallback" in events
    assert "openrouter_exhausted" in events
    assert "openrouter_skip_model_circuit_open" in events
    assert "openrouter_downgrade_json_schema_to_object" in events


def test_audit_no_op_without_callback() -> None:
    """No-audit callback path must not raise."""
    h = _make_handler()
    # All logging methods should be no-ops without an audit callback.
    h.log_attempt(1, "m")
    h.log_success(1, "m", 200, 10, structured_output_used=False, structured_output_mode=None)
    h.log_error(1, "m", 500, "boom")
    h.log_fallback("m1", "m2")


# ---------------------------------------------------------------------------
# build_error_result — shape sanity
# ---------------------------------------------------------------------------


def test_build_error_result_produces_llm_call_result_with_error_status() -> None:
    h = _make_handler()
    res = h.build_error_result(
        model="m1",
        text="server says no",
        data={"error": {"message": "server says no"}},
        usage={"prompt_tokens": 10, "completion_tokens": 0},
        latency=42,
        error_message="HTTP 500: server says no",
        headers={"Authorization": "REDACTED"},
        messages=[{"role": "user", "content": "hi"}],
        error_context={"status_code": 500},
    )
    assert res.status.value == "error"
    assert res.model == "m1"
    assert res.tokens_prompt == 10 and res.tokens_completion == 0
    assert res.latency_ms == 42
    assert "server says no" in (res.error_text or "")
