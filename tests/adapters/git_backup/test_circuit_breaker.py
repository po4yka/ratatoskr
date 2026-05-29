"""Characterization tests for the storage circuit breaker (port of CircuitBreakerTest.kt)."""

from __future__ import annotations

from app.adapters.git_backup.circuit_breaker import StorageCircuitBreaker
from app.adapters.git_backup.errors import ErrorCategory

STORAGE = ErrorCategory.STORAGE_ERROR
NETWORK = ErrorCategory.NETWORK_ERROR
AUTH = ErrorCategory.AUTH_ERROR


def test_starts_closed() -> None:
    assert StorageCircuitBreaker(threshold=3).is_open() is False


def test_below_threshold_stays_closed() -> None:
    breaker = StorageCircuitBreaker(threshold=3)
    breaker.record_failure(STORAGE)
    breaker.record_failure(STORAGE)
    assert breaker.is_open() is False


def test_at_threshold_opens() -> None:
    breaker = StorageCircuitBreaker(threshold=3)
    breaker.record_failure(STORAGE)
    breaker.record_failure(STORAGE)
    tripped = breaker.record_failure(STORAGE)
    assert tripped is True
    assert breaker.is_open() is True


def test_returns_true_only_on_trip_call() -> None:
    breaker = StorageCircuitBreaker(threshold=2)
    breaker.record_failure(STORAGE)
    first_trip = breaker.record_failure(STORAGE)
    second_call = breaker.record_failure(STORAGE)
    assert first_trip is True
    assert second_call is False


def test_non_storage_failure_resets_streak() -> None:
    breaker = StorageCircuitBreaker(threshold=3)
    breaker.record_failure(STORAGE)
    breaker.record_failure(STORAGE)
    breaker.record_failure(NETWORK)
    breaker.record_failure(STORAGE)
    breaker.record_failure(STORAGE)
    assert breaker.is_open() is False


def test_non_storage_never_opens() -> None:
    breaker = StorageCircuitBreaker(threshold=3)
    for _ in range(10):
        breaker.record_failure(NETWORK)
    assert breaker.is_open() is False


def test_success_resets_streak() -> None:
    breaker = StorageCircuitBreaker(threshold=3)
    breaker.record_failure(STORAGE)
    breaker.record_failure(STORAGE)
    breaker.record_success()
    breaker.record_failure(STORAGE)
    breaker.record_failure(STORAGE)
    assert breaker.is_open() is False


def test_stays_open_after_trip() -> None:
    breaker = StorageCircuitBreaker(threshold=1)
    breaker.record_failure(STORAGE)
    assert breaker.is_open() is True
    breaker.record_success()
    breaker.record_failure(NETWORK)
    assert breaker.is_open() is True


def test_auth_error_resets_streak() -> None:
    breaker = StorageCircuitBreaker(threshold=3)
    breaker.record_failure(STORAGE)
    breaker.record_failure(STORAGE)
    breaker.record_failure(AUTH)
    breaker.record_failure(STORAGE)
    breaker.record_failure(STORAGE)
    assert breaker.is_open() is False


def test_threshold_one_trips_on_first() -> None:
    breaker = StorageCircuitBreaker(threshold=1)
    tripped = breaker.record_failure(STORAGE)
    assert tripped is True
    assert breaker.is_open() is True
