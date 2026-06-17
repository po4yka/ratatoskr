"""``SummaryCachePort`` adapter -- wraps ``LLMSummaryCache`` for the summarize graph.

Adapter layer (``app.adapters``): may import concrete infrastructure. Wired at the
composition root (:mod:`app.di.graphs`) into
:class:`~app.application.graphs.summarize.deps.SummarizeDeps` as
``summary_cache``, so graph nodes never import this module
(``application-no-outward``).

The key scheme mirrors ``LLMSummaryCache.get_cached_summary`` /
``write_summary_cache`` exactly:
  ``("llm", prompt_version, lang_key, url_hash)``
so cache entries produced by the legacy interactive path are reusable by the
graph path and vice-versa (zero key drift).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.services.summarization.llm_response_workflow_attempts import (
    summary_has_content,
)
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.application.ports.cache import CachePort

logger = get_logger(__name__)

_REQUIRED_FIELDS = ("tldr", "summary_250", "summary_1000")


class SummaryCacheAdapter:
    """Thin shim over :class:`~app.adapters.content.llm_summarizer_cache.LLMSummaryCache`.

    Implements :class:`~app.application.ports.summary_cache.SummaryCachePort` using
    the same Redis key scheme as the legacy path.
    """

    def __init__(
        self,
        *,
        cache: CachePort,
        prompt_version: str,
        ttl_seconds: int = 7_200,
    ) -> None:
        self._cache = cache
        self._prompt_version = prompt_version
        self._ttl_seconds = ttl_seconds

    async def get(self, url_hash: str, lang: str) -> dict[str, Any] | None:
        """Return a cached summary or None on miss / disabled / validation failure."""
        if not url_hash or not self._cache.enabled:
            return None
        lang_key = lang or "auto"
        cached = await self._cache.get_json("llm", self._prompt_version, lang_key, url_hash)
        if not isinstance(cached, dict):
            return None
        if not summary_has_content(cached, required_fields=_REQUIRED_FIELDS):
            logger.debug(
                "summary_cache_adapter_missing_fields",
                extra={"url_hash": url_hash, "lang": lang_key},
            )
            return None
        logger.info(
            "summary_cache_adapter_hit",
            extra={"url_hash": url_hash, "lang": lang_key},
        )
        return cached

    async def set(self, url_hash: str, lang: str, summary: dict[str, Any]) -> None:
        """Store summary in cache; no-op when cache disabled or summary empty."""
        if not url_hash or not self._cache.enabled:
            return
        if not summary or not isinstance(summary, dict):
            return
        lang_key = lang or "auto"
        await self._cache.set_json(
            value=summary,
            ttl_seconds=self._ttl_seconds,
            parts=("llm", self._prompt_version, lang_key, url_hash),
        )
