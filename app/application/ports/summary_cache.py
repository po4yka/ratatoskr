"""Summary cache port (GAP 2) -- async get/set for Redis-backed LLM summary cache.

Typed ONLY against app.core (application-no-outward). The concrete adapter
wraps ``LLMSummaryCache`` and lives in ``app.adapters.content.summary_cache_adapter``;
the port is wired into :class:`~app.application.graphs.summarize.deps.SummarizeDeps`
at the composition root (:mod:`app.di.graphs`).

The key contract mirrors the legacy ``LLMSummaryCache.get_cached_summary`` /
``write_summary_cache`` behaviour: the key is built over ``url_hash`` (the sha256
of the normalized URL, the codebase's idempotence key) + ``lang`` +
``prompt_version`` inside the adapter, so the port surface stays minimal (just the
semantic inputs the caller already has).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SummaryCachePort(Protocol):
    """Async read/write port for the LLM summary cache (Redis-backed)."""

    async def get(self, url_hash: str, lang: str) -> dict[str, Any] | None:
        """Return a cached summary dict or None on miss / disabled cache."""
        ...

    async def set(self, url_hash: str, lang: str, summary: dict[str, Any]) -> None:
        """Store a summary in the cache; no-op when cache is disabled or summary empty."""
        ...
