"""Behavioral spec for the retry policy (port of RetryPolicyTest.kt via gitout).

Captures backoff math, attempt sequencing, adaptive delay multipliers, HTTP/1.1
fallback surfacing and the SyncFailureException contract. A recording fake sleep
replaces real waiting so the delay sequence is asserted directly.
"""

from __future__ import annotations

import pytest

from app.adapters.git_backup.errors import ErrorCategory
from app.adapters.git_backup.retry import BackoffStrategy, RetryContext, RetryPolicy, SyncFailureException


class SleepRecorder:
    """An injectable sleeper that records requested delays instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[int] = []

    async def __call__(self, ms: int) -> None:
        self.delays.append(ms)


# --- backoff math (calculate_delay, 1-indexed attempt) ---


@pytest.mark.characterization
@pytest.mark.parametrize(
    ("strategy", "attempt", "expected"),
    [
        (BackoffStrategy.LINEAR, 2, 200),
        (BackoffStrategy.LINEAR, 3, 300),
        (BackoffStrategy.EXPONENTIAL, 2, 200),
        (BackoffStrategy.EXPONENTIAL, 3, 400),
        (BackoffStrategy.EXPONENTIAL, 4, 800),
        (BackoffStrategy.CONSTANT, 2, 100),
        (BackoffStrategy.CONSTANT, 4, 100),
    ],
)
def test_calculate_delay(strategy: BackoffStrategy, attempt: int, expected: int) -> None:
    policy = RetryPolicy(max_attempts=5, base_delay_ms=100, backoff_strategy=strategy)
    assert policy.calculate_delay(attempt) == expected


# --- constructor validation ---


@pytest.mark.characterization
@pytest.mark.parametrize("kwargs", [{"max_attempts": 0}, {"base_delay_ms": -1}])
def test_constructor_rejects_invalid_args(kwargs: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


# --- execution: success / retry sequencing ---


@pytest.mark.characterization
async def test_success_on_first_attempt() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay_ms=100, sleep=SleepRecorder())
    seen = []

    async def op(ctx: RetryContext) -> str:
        seen.append(ctx.attempt)
        return "success"

    assert await policy.execute(op, operation_description="t") == "success"
    assert seen == [1]


@pytest.mark.characterization
async def test_zero_base_delay_allowed_and_retries() -> None:
    policy = RetryPolicy(max_attempts=2, base_delay_ms=0, sleep=SleepRecorder())

    async def op(ctx: RetryContext) -> str:
        if ctx.attempt < 2:
            raise RuntimeError("fail")
        return "success"

    assert await policy.execute(op) == "success"


@pytest.mark.characterization
async def test_tracks_attempt_numbers() -> None:
    policy = RetryPolicy(max_attempts=4, base_delay_ms=10, sleep=SleepRecorder())
    seen: list[int] = []

    async def op(ctx: RetryContext) -> str:
        seen.append(ctx.attempt)
        if ctx.attempt < 3:
            raise RuntimeError("not yet")
        return "done"

    await policy.execute(op, operation_description="tracking")
    assert seen == [1, 2, 3]


@pytest.mark.characterization
async def test_max_attempts_one_means_no_retry() -> None:
    policy = RetryPolicy(max_attempts=1, base_delay_ms=10, sleep=SleepRecorder())
    seen: list[int] = []

    async def op(ctx: RetryContext) -> str:
        seen.append(ctx.attempt)
        raise RuntimeError("boom")

    with pytest.raises(SyncFailureException):
        await policy.execute(op)
    assert seen == [1]


# --- exhaustion / failure metadata ---


@pytest.mark.characterization
async def test_exhaustion_raises_with_attempt_count() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay_ms=10, sleep=SleepRecorder())

    async def op(ctx: RetryContext) -> str:
        raise RuntimeError("Persistent failure")

    with pytest.raises(SyncFailureException) as exc:
        await policy.execute(op, operation_description="failing")
    assert exc.value.attempt_count == 3


@pytest.mark.characterization
async def test_records_error_categories() -> None:
    policy = RetryPolicy(max_attempts=2, base_delay_ms=10, sleep=SleepRecorder())

    async def op(ctx: RetryContext) -> str:
        raise RuntimeError("connection reset by peer")

    with pytest.raises(SyncFailureException) as exc:
        await policy.execute(op)
    assert exc.value.error_categories
    assert exc.value.error_categories[0] is ErrorCategory.NETWORK_ERROR


@pytest.mark.characterization
async def test_non_retryable_stops_immediately() -> None:
    policy = RetryPolicy(max_attempts=6, base_delay_ms=10, sleep=SleepRecorder())
    seen: list[int] = []

    async def op(ctx: RetryContext) -> str:
        seen.append(ctx.attempt)
        raise RuntimeError("couldn't find remote ref refs/heads/main")

    with pytest.raises(SyncFailureException) as exc:
        await policy.execute(op, operation_description="repo")
    assert seen == [1]  # REPOSITORY_ERROR is non-retryable
    assert exc.value.attempt_count == 1
    assert exc.value.error_categories[0] is ErrorCategory.REPOSITORY_ERROR


# --- adaptive backoff + HTTP/1.1 fallback ---


@pytest.mark.characterization
async def test_adaptive_delay_multiplier_applied_to_sequence() -> None:
    # NETWORK_ERROR has a 2.0 delay multiplier. LINEAR base 100:
    # before attempt 2: calc(2)=200 * 2.0 = 400; before attempt 3: calc(3)=300 * 2.0 = 600.
    recorder = SleepRecorder()
    policy = RetryPolicy(
        max_attempts=3,
        base_delay_ms=100,
        backoff_strategy=BackoffStrategy.LINEAR,
        sleep=recorder,
    )

    async def op(ctx: RetryContext) -> str:
        raise RuntimeError("connection reset by peer")

    with pytest.raises(SyncFailureException):
        await policy.execute(op, operation_description="net")
    assert recorder.delays == [400, 600]


@pytest.mark.characterization
async def test_http1_fallback_surfaced_after_network_error() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay_ms=0, sleep=SleepRecorder())
    contexts: list[RetryContext] = []

    async def op(ctx: RetryContext) -> str:
        contexts.append(ctx)
        if ctx.attempt < 2:
            raise RuntimeError("connection reset by peer")
        return "ok"

    result = await policy.execute_with_result(op)
    assert result.value == "ok"
    assert result.used_http1_fallback is True
    assert contexts[1].should_use_http1_fallback is True
