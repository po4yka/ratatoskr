"""Cache helper for LLM summarization."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from app.adapters.content.llm_response_workflow_attempts import summary_has_content
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.application.ports.cache import CachePort as RedisCache

logger = get_logger(__name__)


class LLMSummaryCache:
    """Handle Redis-backed caching for summaries, insights, and translations."""

    def __init__(
        self,
        *,
        cache: RedisCache,
        cfg: Any,
        prompt_version: str,
        insights_has_content: Callable[[dict[str, Any]], bool],
    ) -> None:
        self._cache = cache
        self._cfg = cfg
        self._prompt_version = prompt_version
        self._insights_has_content = insights_has_content

    async def get_cached_summary(
        self,
        url_hash: str | None,
        chosen_lang: str | None,
        model_name: str,
        correlation_id: str | None,
    ) -> dict[str, Any] | None:
        """Return cached summary if present and valid."""
        if not url_hash or not self._cache.enabled:
            return None

        lang_key = chosen_lang or "auto"
        # Model-agnostic key: a summary produced by a fallback model is reusable
        # by a later request regardless of which model is currently primary.
        cached = await self._cache.get_json("llm", self._prompt_version, lang_key, url_hash)
        if not isinstance(cached, dict):
            return None

        if not summary_has_content(cached, required_fields=("tldr", "summary_250", "summary_1000")):
            logger.debug(
                "llm_cache_missing_fields",
                extra={"cid": correlation_id, "lang": lang_key, "model": model_name},
            )
            return None

        return cached

    async def write_summary_cache(
        self, url_hash: str, model_name: str, chosen_lang: str, summary: dict[str, Any]
    ) -> None:
        """Persist shaped summary into Redis cache."""
        if not self._cache.enabled:
            return
        if not summary or not isinstance(summary, dict):
            return

        await self._cache.set_json(
            value=summary,
            ttl_seconds=getattr(self._cfg.redis, "llm_ttl_seconds", 7_200),
            parts=("llm", self._prompt_version, chosen_lang or "auto", url_hash),
        )

    @staticmethod
    def _hash_cache_key(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def build_topics_cache_key(self, topics: list[str], tags: list[str]) -> str:
        normalized = sorted(
            {
                item.strip().lower()
                for item in (topics + tags)
                if isinstance(item, str) and item.strip()
            }
        )
        if not normalized:
            return ""
        return self._hash_cache_key("|".join(normalized))

    async def get_cached_insights(
        self,
        url_hash: str | None,
        chosen_lang: str | None,
        model_name: str,
        correlation_id: str | None,
    ) -> dict[str, Any] | None:
        if not url_hash or not self._cache.enabled:
            return None

        lang_key = chosen_lang or "auto"
        cached = await self._cache.get_json(
            "llm", "insights", self._prompt_version, lang_key, url_hash
        )
        if not isinstance(cached, dict):
            return None
        if not self._insights_has_content(cached):
            logger.debug(
                "insights_cache_missing_fields",
                extra={"cid": correlation_id, "lang": lang_key, "model": model_name},
            )
            return None
        return cached

    async def write_insights_cache(
        self, url_hash: str, model_name: str, chosen_lang: str, insights: dict[str, Any]
    ) -> None:
        if not self._cache.enabled:
            return
        if not insights or not isinstance(insights, dict):
            return

        await self._cache.set_json(
            value=insights,
            ttl_seconds=getattr(self._cfg.redis, "llm_ttl_seconds", 7_200),
            parts=(
                "llm",
                "insights",
                self._prompt_version,
                chosen_lang or "auto",
                url_hash,
            ),
        )

    async def get_cached_translation(
        self,
        url_hash: str | None,
        source_lang: str | None,
        model_name: str,
        correlation_id: str | None,
    ) -> str | None:
        if not url_hash or not self._cache.enabled:
            return None

        lang_key = source_lang or "auto"
        cached = await self._cache.get_json(
            "llm", "translation_ru", self._prompt_version, lang_key, url_hash
        )
        if isinstance(cached, str) and cached.strip():
            return cached

        logger.debug(
            "translation_cache_miss",
            extra={"cid": correlation_id, "lang": lang_key, "model": model_name},
        )
        return None

    async def write_translation_cache(
        self, url_hash: str, model_name: str, source_lang: str, translation: str
    ) -> None:
        if not self._cache.enabled:
            return
        if not translation or not isinstance(translation, str):
            return

        await self._cache.set_json(
            value=translation,
            ttl_seconds=getattr(self._cfg.redis, "llm_ttl_seconds", 7_200),
            parts=(
                "llm",
                "translation_ru",
                self._prompt_version,
                source_lang or "auto",
                url_hash,
            ),
        )

    async def get_cached_custom_article(
        self,
        url_hash: str | None,
        chosen_lang: str | None,
        model_name: str,
        topics_key: str,
        correlation_id: str | None,
    ) -> dict[str, Any] | None:
        if not url_hash or not topics_key or not self._cache.enabled:
            return None

        lang_key = chosen_lang or "auto"
        cached = await self._cache.get_json(
            "llm",
            "custom_article",
            self._prompt_version,
            lang_key,
            url_hash,
            topics_key,
        )
        if not isinstance(cached, dict):
            return None
        return cached

    async def write_custom_article_cache(
        self,
        url_hash: str,
        model_name: str,
        chosen_lang: str,
        topics_key: str,
        article: dict[str, Any],
    ) -> None:
        if not self._cache.enabled:
            return
        if not topics_key or not isinstance(article, dict):
            return

        await self._cache.set_json(
            value=article,
            ttl_seconds=getattr(self._cfg.redis, "llm_ttl_seconds", 7_200),
            parts=(
                "llm",
                "custom_article",
                self._prompt_version,
                chosen_lang or "auto",
                url_hash,
                topics_key,
            ),
        )

    def build_cache_stub(self, model_name: str) -> Any:
        """LLM stub used when summary is served from cache."""
        return type(
            "LLMCacheStub",
            (),
            {
                "status": "ok",
                "latency_ms": 0,
                "model": model_name,
                "structured_output_used": True,
                "structured_output_mode": self._cfg.openrouter.structured_output_mode,
            },
        )()
