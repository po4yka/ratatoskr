"""Cache port — minimal async key/value cache abstraction."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CachePort(Protocol):
    """Minimal async JSON cache interface used by content adapters."""

    @property
    def enabled(self) -> bool:
        """Return True when the cache backend is configured and active."""

    async def get_json(self, *parts: str) -> Any | None:
        """Fetch a JSON value; return None on miss or any error."""

    async def set_json(self, *, value: Any, ttl_seconds: int, parts: Any) -> bool:
        """Store a JSON value with TTL; return False on failure."""

    async def clear(self) -> int:
        """Clear all cached keys; return the number of deleted keys."""
