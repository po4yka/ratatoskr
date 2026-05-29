"""Storage-failure circuit breaker (port of StorageCircuitBreaker in Engine.kt).

Trips after a configurable number of *consecutive* ``STORAGE_ERROR`` failures, so a
shared-infrastructure fault (full/read-only/unmounted volume) aborts the run with one
alert instead of an error storm. Any non-storage failure or a success resets the
streak; once open the breaker stays open.
"""

from __future__ import annotations

from app.adapters.git_backup.errors import ErrorCategory


class StorageCircuitBreaker:
    def __init__(self, threshold: int) -> None:
        self._threshold = threshold
        self._consecutive_storage_failures = 0
        self._open = False

    def is_open(self) -> bool:
        """True once the breaker has tripped (the run should abort)."""
        return self._open

    def record_success(self) -> None:
        """Reset the consecutive-failure streak (does not close an open breaker)."""
        self._consecutive_storage_failures = 0

    def record_failure(self, category: ErrorCategory) -> bool:
        """Record a failure; return True only on the call that first trips the breaker.

        Only ``STORAGE_ERROR`` increments the streak; any other category resets it.
        """
        if category is not ErrorCategory.STORAGE_ERROR:
            self._consecutive_storage_failures = 0
            return False

        self._consecutive_storage_failures += 1
        if not self._open and self._consecutive_storage_failures >= self._threshold:
            self._open = True
            return True
        return False
