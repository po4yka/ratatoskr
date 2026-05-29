"""Retry policy with adaptive, category-aware backoff.

Port of ``RetryPolicy.kt`` via gitout. Enums, the context/result containers and
the ``SyncFailureException`` shape are the spec; backoff math and ``execute`` are
the implementation.

The Kotlin original uses ``kotlinx.coroutines.delay``; here ``execute`` takes an
injectable ``sleep`` coroutine so tests can assert the delay sequence without real
waiting.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Generic, TypeVar

from app.adapters.git_backup import errors

if TYPE_CHECKING:
    from app.adapters.git_backup.errors import ErrorCategory

T = TypeVar("T")


class BackoffStrategy(Enum):
    """Strategy for the delay between retry attempts."""

    LINEAR = "LINEAR"  # baseDelayMs * attempt
    EXPONENTIAL = "EXPONENTIAL"  # baseDelayMs * 2^(attempt-1)
    CONSTANT = "CONSTANT"  # baseDelayMs


class SyncFailureException(Exception):
    """Raised when an operation fails after all retry attempts are exhausted."""

    def __init__(
        self,
        message: str,
        *,
        error_categories: list[ErrorCategory],
        attempt_count: int,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.error_categories = error_categories
        self.attempt_count = attempt_count
        self.__cause__ = cause


@dataclass(frozen=True)
class RetryContext:
    attempt: int
    max_attempts: int
    should_use_http1_fallback: bool
    last_error_category: ErrorCategory | None
    is_retry: bool


@dataclass(frozen=True)
class RetryResult(Generic[T]):
    value: T
    attempts: int
    used_http1_fallback: bool
    error_categories: list[ErrorCategory] = field(default_factory=list)


# A sleeper coroutine: awaits for the given number of milliseconds.
Sleeper = Callable[[int], Awaitable[None]]


class RetryPolicy:
    def __init__(
        self,
        *,
        max_attempts: int = 6,
        base_delay_ms: int = 5000,
        backoff_strategy: BackoffStrategy = BackoffStrategy.LINEAR,
        adaptive_retry: bool = True,
        sleep: Sleeper | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, was: {max_attempts}")
        if base_delay_ms < 0:
            raise ValueError(f"base_delay_ms must be non-negative, was: {base_delay_ms}")
        self.max_attempts = max_attempts
        self.base_delay_ms = base_delay_ms
        self.backoff_strategy = backoff_strategy
        self.adaptive_retry = adaptive_retry
        self._sleep = sleep

    def calculate_delay(self, attempt: int) -> int:
        """Delay in ms before ``attempt`` (1-indexed), per the backoff strategy."""
        if self.backoff_strategy is BackoffStrategy.LINEAR:
            return self.base_delay_ms * attempt
        if self.backoff_strategy is BackoffStrategy.EXPONENTIAL:
            return self.base_delay_ms * (1 << (attempt - 1))
        return self.base_delay_ms  # CONSTANT

    async def _do_sleep(self, ms: int) -> None:
        if self._sleep is not None:
            await self._sleep(ms)
        else:
            await asyncio.sleep(ms / 1000)

    async def execute_with_result(
        self,
        operation: Callable[[RetryContext], Awaitable[T]],
        *,
        operation_description: str | None = None,
    ) -> RetryResult[T]:
        """Run ``operation`` with retries; raise SyncFailureException when exhausted."""
        last_exception: BaseException | None = None
        use_http1_fallback = False
        last_category: ErrorCategory | None = None
        categories: list[ErrorCategory] = []
        actual_attempts = 0

        for attempt in range(1, self.max_attempts + 1):
            actual_attempts = attempt
            try:
                if attempt > 1:
                    base_delay = self.calculate_delay(attempt)
                    multiplier = (
                        errors.delay_multiplier(last_category)
                        if self.adaptive_retry and last_category is not None
                        else 1.0
                    )
                    await self._do_sleep(int(base_delay * multiplier))

                context = RetryContext(
                    attempt=attempt,
                    max_attempts=self.max_attempts,
                    should_use_http1_fallback=use_http1_fallback,
                    last_error_category=last_category,
                    is_retry=attempt > 1,
                )
                value = await operation(context)
                return RetryResult(
                    value=value,
                    attempts=attempt,
                    used_http1_fallback=use_http1_fallback,
                    error_categories=categories,
                )
            except Exception as exc:  # classified and re-raised below
                last_exception = exc
                if self.adaptive_retry:
                    last_category = errors.classify(str(exc))
                    categories.append(last_category)
                    if not use_http1_fallback and errors.should_use_http1_fallback(last_category):
                        use_http1_fallback = True
                    if not errors.is_retryable(last_category):
                        break
                if attempt == self.max_attempts:
                    break

        description = operation_description or "operation"
        distinct = list(dict.fromkeys(categories))
        category_info = (
            f" (error categories: {', '.join(c.name for c in distinct)})" if distinct else ""
        )
        raise SyncFailureException(
            f"Failed to complete {description} after {actual_attempts} attempts{category_info}",
            error_categories=list(categories),
            attempt_count=actual_attempts,
            cause=last_exception,
        )

    async def execute(
        self,
        operation: Callable[[RetryContext], Awaitable[T]],
        *,
        operation_description: str | None = None,
    ) -> T:
        result = await self.execute_with_result(
            operation, operation_description=operation_description
        )
        return result.value

    def __repr__(self) -> str:
        return (
            f"RetryPolicy(max_attempts={self.max_attempts}, base_delay_ms={self.base_delay_ms}, "
            f"backoff_strategy={self.backoff_strategy.value}, adaptive_retry={self.adaptive_retry})"
        )
