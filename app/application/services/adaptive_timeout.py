"""Adaptive timeout service for URL processing.

This service estimates appropriate timeouts based on historical latency data,
falling back gracefully when insufficient data is available.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger
from app.core.url_utils import extract_domain

if TYPE_CHECKING:
    from app.config.adaptive_timeout import AdaptiveTimeoutConfig

logger = get_logger(__name__)


@dataclass(frozen=True)
class TimeoutEstimate:
    """Result of timeout estimation with confidence metadata."""

    timeout_sec: float
    confidence: float  # 0.0-1.0 based on sample size
    source: str  # "domain", "model", "content", "combined", "global", "default"
    sample_count: int = 0
    p95_ms: float | None = None

    def __post_init__(self) -> None:
        # Validate confidence is in range
        if not 0.0 <= self.confidence <= 1.0:
            object.__setattr__(self, "confidence", max(0.0, min(1.0, self.confidence)))


@dataclass
class CacheEntry:
    """Cached latency statistics with TTL tracking."""

    stats: Any
    timestamp: float
    key: str


@dataclass
class TimeoutCache:
    """In-memory cache for latency statistics with TTL-based expiry."""

    ttl_sec: int = 300
    domain_stats: dict[str, CacheEntry] = field(default_factory=dict)
    model_stats: dict[str, CacheEntry] = field(default_factory=dict)
    global_stats: CacheEntry | None = None
    combined_stats: dict[str, CacheEntry] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _is_expired(self, entry: CacheEntry) -> bool:
        """Check if a cache entry has expired."""
        return time.time() - entry.timestamp > self.ttl_sec

    async def get_domain(self, domain: str) -> Any | None:
        """Get cached domain stats if not expired."""
        async with self._lock:
            entry = self.domain_stats.get(domain)
            if entry and not self._is_expired(entry):
                return entry.stats
            return None

    async def set_domain(self, domain: str, stats: Any) -> None:
        """Cache domain stats."""
        async with self._lock:
            self.domain_stats[domain] = CacheEntry(stats=stats, timestamp=time.time(), key=domain)

    async def get_model(self, model: str) -> Any | None:
        """Get cached model stats if not expired."""
        async with self._lock:
            entry = self.model_stats.get(model)
            if entry and not self._is_expired(entry):
                return entry.stats
            return None

    async def set_model(self, model: str, stats: Any) -> None:
        """Cache model stats."""
        async with self._lock:
            self.model_stats[model] = CacheEntry(stats=stats, timestamp=time.time(), key=model)

    async def get_global(self) -> Any | None:
        """Get cached global stats if not expired."""
        async with self._lock:
            if self.global_stats and not self._is_expired(self.global_stats):
                return self.global_stats.stats
            return None

    async def set_global(self, stats: Any) -> None:
        """Cache global stats."""
        async with self._lock:
            self.global_stats = CacheEntry(stats=stats, timestamp=time.time(), key="global")

    async def get_combined(self, domain: str) -> Any | None:
        """Get cached combined (crawl + LLM) stats for a domain."""
        async with self._lock:
            entry = self.combined_stats.get(domain)
            if entry and not self._is_expired(entry):
                return entry.stats
            return None

    async def set_combined(self, domain: str, stats: Any) -> None:
        """Cache combined stats for a domain."""
        async with self._lock:
            self.combined_stats[domain] = CacheEntry(stats=stats, timestamp=time.time(), key=domain)


def _extract_domain(url: str | None) -> str | None:
    """Extract domain from URL, normalizing www prefix."""
    domain = extract_domain(url)
    if url and domain is None:
        logger.debug("adaptive_timeout_domain_extraction_failed", exc_info=True)
    return domain


class AdaptiveTimeoutService:
    """Service for computing adaptive timeouts based on historical latency data.

    The service uses a priority-based fallback strategy:
    1. Combined domain stats (crawl + LLM time for full pipeline)
    2. Domain-specific crawl stats
    3. Model-specific LLM stats
    4. Content-length based estimation
    5. Global stats across all domains/models
    6. Default configuration value
    """

    def __init__(
        self,
        config: AdaptiveTimeoutConfig,
        repository: Any,
    ) -> None:
        self._config = config
        self._cache = TimeoutCache(ttl_sec=config.cache_ttl_sec)
        self._initialized = False
        self._repo = repository

    @property
    def enabled(self) -> bool:
        """Check if adaptive timeout is enabled."""
        return self._config.enabled

    def _compute_confidence(self, sample_count: int) -> float:
        """Compute confidence score based on sample count.

        Confidence increases logarithmically from 0 to 1 as samples approach
        and exceed min_samples. Reaches ~0.9 at min_samples.
        """
        if sample_count <= 0:
            return 0.0

        min_samples = self._config.min_samples
        if sample_count >= min_samples * 2:
            return 1.0

        # Linear scaling for smooth confidence curve
        ratio = sample_count / min_samples
        if ratio >= 1.0:
            # Above threshold: 0.9 to 1.0
            return 0.9 + 0.1 * min(1.0, (ratio - 1.0))
        # Below threshold: 0 to 0.9 scaled by ratio
        return 0.9 * ratio

    def _stats_to_timeout(self, stats: Any, source: str) -> TimeoutEstimate | None:
        """Convert latency stats to a timeout estimate.

        Applies safety margin and clamps to configured bounds.
        """
        if stats.p95_ms is None or stats.sample_count < self._config.min_samples:
            return None

        # Convert P95 milliseconds to seconds with safety margin
        raw_timeout = (stats.p95_ms / 1000.0) * self._config.safety_margin

        # Clamp to configured bounds
        timeout = max(
            self._config.min_timeout_sec,
            min(self._config.max_timeout_sec, raw_timeout),
        )

        confidence = self._compute_confidence(stats.sample_count)

        return TimeoutEstimate(
            timeout_sec=timeout,
            confidence=confidence,
            source=source,
            sample_count=stats.sample_count,
            p95_ms=stats.p95_ms,
        )

    def _content_length_estimate(self, content_length: int | None) -> TimeoutEstimate:
        """Estimate timeout based on content length.

        Longer content requires more LLM processing time.
        Formula: 60s base + ~1s per 1000 chars.
        """
        if content_length is None or content_length <= 0:
            return TimeoutEstimate(
                timeout_sec=self._config.default_timeout_sec,
                confidence=0.3,
                source="default",
            )

        # Base timeout + additional time per 10k characters
        estimated = (
            self._config.content_base_timeout_sec
            + (content_length / 10000) * self._config.content_per_10k_chars_sec
        )

        # Clamp to bounds
        timeout = max(
            self._config.min_timeout_sec,
            min(self._config.max_timeout_sec, estimated),
        )

        return TimeoutEstimate(
            timeout_sec=timeout,
            confidence=0.5,  # Medium confidence for content-based estimate
            source="content",
        )

    async def get_timeout(
        self,
        url: str | None = None,
        domain: str | None = None,
        model: str | None = None,
        content_length: int | None = None,
    ) -> TimeoutEstimate:
        """Get adaptive timeout estimate using priority-based fallback.

        Args:
            url: The URL being processed (domain will be extracted if not provided)
            domain: Pre-extracted domain (optional optimization)
            model: The LLM model being used
            content_length: Estimated content length in characters

        Returns:
            TimeoutEstimate with timeout value, confidence, and source
        """
        if not self._config.enabled:
            return TimeoutEstimate(
                timeout_sec=self._config.default_timeout_sec,
                confidence=1.0,
                source="disabled",
            )

        # Extract domain from URL if not provided
        if domain is None and url:
            domain = _extract_domain(url)

        # 1. Try combined domain stats (full pipeline: crawl + LLM)
        if domain:
            estimate = await self._get_combined_domain_timeout(domain)
            if estimate:
                # If we have content length, ensure timeout covers it
                if content_length and content_length > 0:
                    content_est = self._content_length_estimate(content_length)
                    if content_est.timeout_sec > estimate.timeout_sec:
                        estimate = TimeoutEstimate(
                            timeout_sec=content_est.timeout_sec,
                            confidence=estimate.confidence,
                            source=f"{estimate.source}+content",
                            sample_count=estimate.sample_count,
                            p95_ms=estimate.p95_ms,
                        )

                logger.debug(
                    "adaptive_timeout_computed",
                    extra={
                        "source": estimate.source,
                        "timeout_sec": estimate.timeout_sec,
                        "confidence": estimate.confidence,
                        "domain": domain,
                        "sample_count": estimate.sample_count,
                        "p95_ms": estimate.p95_ms,
                    },
                )
                return estimate

        # 2. Try domain-specific crawl stats
        if domain:
            estimate = await self._get_domain_timeout(domain)
            if estimate:
                logger.debug(
                    "adaptive_timeout_computed",
                    extra={
                        "source": estimate.source,
                        "timeout_sec": estimate.timeout_sec,
                        "confidence": estimate.confidence,
                        "domain": domain,
                        "sample_count": estimate.sample_count,
                        "p95_ms": estimate.p95_ms,
                    },
                )
                return estimate

        # 3. Try model-specific LLM stats
        if model:
            estimate = await self._get_model_timeout(model)
            if estimate:
                logger.debug(
                    "adaptive_timeout_computed",
                    extra={
                        "source": estimate.source,
                        "timeout_sec": estimate.timeout_sec,
                        "confidence": estimate.confidence,
                        "model": model,
                        "sample_count": estimate.sample_count,
                        "p95_ms": estimate.p95_ms,
                    },
                )
                return estimate

        # 4. Try content-length based estimation
        if content_length and content_length > 0:
            estimate = self._content_length_estimate(content_length)
            logger.debug(
                "adaptive_timeout_computed",
                extra={
                    "source": estimate.source,
                    "timeout_sec": estimate.timeout_sec,
                    "confidence": estimate.confidence,
                    "content_length": content_length,
                },
            )
            return estimate

        # 5. Try global stats
        estimate = await self._get_global_timeout()
        if estimate:
            logger.debug(
                "adaptive_timeout_computed",
                extra={
                    "source": estimate.source,
                    "timeout_sec": estimate.timeout_sec,
                    "confidence": estimate.confidence,
                    "sample_count": estimate.sample_count,
                    "p95_ms": estimate.p95_ms,
                },
            )
            return estimate

        # 6. Fall back to default
        logger.debug(
            "adaptive_timeout_computed",
            extra={
                "source": "default",
                "timeout_sec": self._config.default_timeout_sec,
                "confidence": 0.3,
                "reason": "no_historical_data",
            },
        )
        return TimeoutEstimate(
            timeout_sec=self._config.default_timeout_sec,
            confidence=0.3,
            source="default",
        )

    async def _get_combined_domain_timeout(self, domain: str) -> TimeoutEstimate | None:
        """Get timeout based on combined crawl + LLM latency for domain."""
        stats = await self._cache.get_combined(domain)
        if stats is None:
            try:
                stats = await self._repo.async_get_combined_url_processing_stats(
                    domain, days=self._config.history_days
                )
                await self._cache.set_combined(domain, stats)
            except Exception as e:
                logger.warning(
                    "failed_to_get_combined_stats",
                    extra={"domain": domain, "error": str(e)},
                )
                return None

        return self._stats_to_timeout(stats, "combined")

    async def _get_domain_timeout(self, domain: str) -> TimeoutEstimate | None:
        """Get timeout based on domain-specific crawl latency."""
        stats = await self._cache.get_domain(domain)
        if stats is None:
            try:
                stats = await self._repo.async_get_domain_latency_stats(
                    domain, days=self._config.history_days
                )
                await self._cache.set_domain(domain, stats)
            except Exception as e:
                logger.warning(
                    "failed_to_get_domain_stats",
                    extra={"domain": domain, "error": str(e)},
                )
                return None

        return self._stats_to_timeout(stats, "domain")

    async def _get_model_timeout(self, model: str) -> TimeoutEstimate | None:
        """Get timeout based on model-specific LLM latency."""
        stats = await self._cache.get_model(model)
        if stats is None:
            try:
                stats = await self._repo.async_get_model_latency_stats(
                    model, days=self._config.history_days
                )
                await self._cache.set_model(model, stats)
            except Exception as e:
                logger.warning(
                    "failed_to_get_model_stats",
                    extra={"model": model, "error": str(e)},
                )
                return None

        return self._stats_to_timeout(stats, "model")

    async def _get_global_timeout(self) -> TimeoutEstimate | None:
        """Get timeout based on global latency stats."""
        stats = await self._cache.get_global()
        if stats is None:
            try:
                stats = await self._repo.async_get_global_latency_stats(
                    days=self._config.history_days
                )
                await self._cache.set_global(stats)
            except Exception as e:
                logger.warning(
                    "failed_to_get_global_stats",
                    extra={"error": str(e)},
                )
                return None

        return self._stats_to_timeout(stats, "global")

    async def get_llm_timeout(
        self, model: str | None = None, estimated_tokens: int | None = None
    ) -> float:
        """Get timeout specifically for LLM summarization.

        Args:
            model: The LLM model being used
            estimated_tokens: Estimated output tokens

        Returns:
            Timeout in seconds
        """
        estimate = await self.get_timeout(model=model)
        # LLM typically uses ~50-70% of total processing time
        return max(self._config.min_timeout_sec, estimate.timeout_sec * 0.6)

    async def warm_cache(self) -> None:
        """Pre-populate cache with global stats on startup.

        Called during bot initialization to ensure fast first-request handling.
        """
        if not self._config.enabled:
            logger.info("adaptive_timeout_disabled_skipping_warmup")
            return

        try:
            # Warm global stats
            global_stats = await self._repo.async_get_global_latency_stats(
                days=self._config.history_days
            )
            await self._cache.set_global(global_stats)

            logger.info(
                "adaptive_timeout_cache_warmed",
                extra={
                    "global_sample_count": global_stats.sample_count,
                    "global_p95_ms": global_stats.p95_ms,
                },
            )
            self._initialized = True
        except Exception as e:
            logger.warning(
                "adaptive_timeout_warmup_failed",
                extra={"error": str(e)},
            )
            # Service remains functional with default fallback

    def get_stats_summary(self) -> dict:
        """Get summary of current cache state for diagnostics."""
        return {
            "enabled": self._config.enabled,
            "initialized": self._initialized,
            "cache_ttl_sec": self._config.cache_ttl_sec,
            "min_timeout_sec": self._config.min_timeout_sec,
            "max_timeout_sec": self._config.max_timeout_sec,
            "default_timeout_sec": self._config.default_timeout_sec,
            "history_days": self._config.history_days,
            "min_samples": self._config.min_samples,
            "cached_domains": len(self._cache.domain_stats),
            "cached_models": len(self._cache.model_stats),
            "cached_combined": len(self._cache.combined_stats),
            "has_global_cache": self._cache.global_stats is not None,
        }
