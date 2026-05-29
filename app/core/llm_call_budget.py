"""Per-request hard cap on LLM provider invocations.

The summary workflow can invoke the LLM provider many times for a single
request: once per attempt in the request list, once per JSON-repair pass, and
(on the instructor path) once per sticky-fallback retry. Each invocation may
itself fan out across the configured fallback cascade. Without an absolute
ceiling the worst case is unbounded in the number of *invocations*, so a
degraded-provider day can turn one request into dozens of cascade runs.

``LLMCallBudget`` is a tiny per-request counter: every code path that is about
to invoke the provider calls :meth:`charge` first. When the configured cap is
reached, :class:`LLMCallCapExceeded` is raised so the caller can stop cleanly
instead of launching another cascade.
"""

from __future__ import annotations


class LLMCallCapExceeded(Exception):
    """Raised when a single request would exceed its hard LLM-call cap."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"per-request LLM call cap ({limit}) exceeded")


class LLMCallBudget:
    """Counts LLM provider invocations for one request and enforces a cap.

    The count tracks provider *invocations* (each may try the configured
    fallback cascade), not individual HTTP sub-calls; the cascade fan-out is
    separately bounded by the model/retry configuration.
    """

    __slots__ = ("_count", "_limit")

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._count = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def count(self) -> int:
        return self._count

    def remaining(self) -> int:
        return max(0, self._limit - self._count)

    def would_exceed(self) -> bool:
        """True if a further charge would exceed the cap (no state change)."""
        return self._count >= self._limit

    def charge(self) -> int:
        """Record one provider invocation; raise once the cap is exhausted."""
        if self._count >= self._limit:
            raise LLMCallCapExceeded(self._limit)
        self._count += 1
        return self._count
