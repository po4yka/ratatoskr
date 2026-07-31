"""Unit tests for the adaptive timeout service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.services.adaptive_timeout import (
    AdaptiveTimeoutService,
    TimeoutCache,
    TimeoutEstimate,
)
from app.config.adaptive_timeout import AdaptiveTimeoutConfig
from app.infrastructure.persistence.repositories.latency_stats_repository import (
    LatencyStats,
    _compute_percentile,
    _extract_domain,
)


class TestComputePercentile:
    """Tests for the percentile computation function."""

    def test_empty_list_returns_none(self) -> None:
        assert _compute_percentile([], 0.5) is None

    def test_single_value_returns_that_value(self) -> None:
        assert _compute_percentile([100], 0.5) == 100.0
        assert _compute_percentile([100], 0.95) == 100.0

    def test_two_values_p50(self) -> None:
        # P50 of [100, 200] should be 150
        result = _compute_percentile([100, 200], 0.5)
        assert result == 150.0

    def test_two_values_p95(self) -> None:
        # P95 of [100, 200] should be close to 195
        result = _compute_percentile([100, 200], 0.95)
        assert result == pytest.approx(195.0)

    def test_multiple_values_p50(self) -> None:
        # P50 of [100, 200, 300, 400, 500] should be 300
        result = _compute_percentile([100, 200, 300, 400, 500], 0.5)
        assert result == 300.0

    def test_multiple_values_p95(self) -> None:
        # P95 of [100, 200, 300, 400, 500] should be close to 480
        result = _compute_percentile([100, 200, 300, 400, 500], 0.95)
        assert result == pytest.approx(480.0)

    def test_unsorted_input_is_sorted(self) -> None:
        # Should handle unsorted input correctly
        result = _compute_percentile([500, 100, 300, 200, 400], 0.5)
        assert result == 300.0

    def test_p0_returns_minimum(self) -> None:
        result = _compute_percentile([100, 200, 300], 0.0)
        assert result == 100.0

    def test_p100_returns_maximum(self) -> None:
        result = _compute_percentile([100, 200, 300], 1.0)
        assert result == 300.0


class TestExtractDomain:
    """Tests for domain extraction from URLs."""

    def test_simple_url(self) -> None:
        assert _extract_domain("https://example.com/page") == "example.com"

    def test_www_prefix_removed(self) -> None:
        assert _extract_domain("https://www.example.com/page") == "example.com"

    def test_subdomain_preserved(self) -> None:
        assert _extract_domain("https://api.example.com/v1") == "api.example.com"

    def test_port_preserved(self) -> None:
        assert _extract_domain("https://example.com:8080/page") == "example.com:8080"

    def test_none_input(self) -> None:
        assert _extract_domain(None) is None

    def test_empty_string(self) -> None:
        assert _extract_domain("") is None

    def test_invalid_url(self) -> None:
        # Should handle gracefully
        result = _extract_domain("not a valid url")
        assert result is not None  # May extract something, just shouldn't crash


class TestLatencyStats:
    """Tests for LatencyStats dataclass."""

    def test_has_sufficient_data_with_enough_samples(self) -> None:
        stats = LatencyStats(p50_ms=100.0, p95_ms=200.0, sample_count=10)
        assert stats.has_sufficient_data is True

    def test_has_sufficient_data_insufficient_samples(self) -> None:
        stats = LatencyStats(p50_ms=100.0, p95_ms=200.0, sample_count=5)
        assert stats.has_sufficient_data is False

    def test_has_sufficient_data_no_p95(self) -> None:
        stats = LatencyStats(p50_ms=100.0, p95_ms=None, sample_count=20)
        assert stats.has_sufficient_data is False

    def test_immutability(self) -> None:
        stats = LatencyStats(p50_ms=100.0, p95_ms=200.0, sample_count=10)
        with pytest.raises(AttributeError):
            stats.p50_ms = 150.0  # type: ignore[misc]


class TestTimeoutEstimate:
    """Tests for TimeoutEstimate dataclass."""

    def test_basic_creation(self) -> None:
        estimate = TimeoutEstimate(
            timeout_sec=300.0,
            confidence=0.85,
            source="domain",
            sample_count=15,
            p95_ms=250000.0,
        )
        assert estimate.timeout_sec == 300.0
        assert estimate.confidence == 0.85
        assert estimate.source == "domain"

    def test_confidence_clamped_high(self) -> None:
        estimate = TimeoutEstimate(timeout_sec=100.0, confidence=1.5, source="test")
        assert estimate.confidence == 1.0

    def test_confidence_clamped_low(self) -> None:
        estimate = TimeoutEstimate(timeout_sec=100.0, confidence=-0.5, source="test")
        assert estimate.confidence == 0.0


class TestTimeoutCache:
    """Tests for TimeoutCache."""

    @pytest.fixture
    def cache(self) -> TimeoutCache:
        return TimeoutCache(ttl_sec=300)

    @pytest.mark.asyncio
    async def test_domain_cache_roundtrip(self, cache: TimeoutCache) -> None:
        stats = LatencyStats(p50_ms=100.0, p95_ms=200.0, sample_count=20)
        await cache.set_domain("example.com", stats)
        result = await cache.get_domain("example.com")
        assert result == stats

    @pytest.mark.asyncio
    async def test_domain_cache_miss(self, cache: TimeoutCache) -> None:
        result = await cache.get_domain("unknown.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_model_cache_roundtrip(self, cache: TimeoutCache) -> None:
        stats = LatencyStats(p50_ms=100.0, p95_ms=200.0, sample_count=20)
        await cache.set_model("gpt-4", stats)
        result = await cache.get_model("gpt-4")
        assert result == stats

    @pytest.mark.asyncio
    async def test_global_cache_roundtrip(self, cache: TimeoutCache) -> None:
        stats = LatencyStats(p50_ms=100.0, p95_ms=200.0, sample_count=20)
        await cache.set_global(stats)
        result = await cache.get_global()
        assert result == stats


class TestAdaptiveTimeoutConfig:
    """Tests for AdaptiveTimeoutConfig validation."""

    def test_default_values(self) -> None:
        config = AdaptiveTimeoutConfig()
        assert config.enabled is True
        assert config.min_timeout_sec == 60.0
        assert config.max_timeout_sec == 1800.0
        assert config.default_timeout_sec == 900.0
        assert config.target_percentile == 0.95
        assert config.safety_margin == 1.3

    def test_enabled_string_parsing(self) -> None:
        config = AdaptiveTimeoutConfig(enabled="true")  # type: ignore[arg-type]
        assert config.enabled is True

        config = AdaptiveTimeoutConfig(enabled="false")  # type: ignore[arg-type]
        assert config.enabled is False

        config = AdaptiveTimeoutConfig(enabled="1")  # type: ignore[arg-type]
        assert config.enabled is True

    def test_invalid_percentile_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"between 0 and 1"):
            AdaptiveTimeoutConfig(target_percentile=1.5)

    def test_invalid_safety_margin_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"at least 1\.0"):
            AdaptiveTimeoutConfig(safety_margin=0.5)


class TestAdaptiveTimeoutService:
    """Tests for AdaptiveTimeoutService."""

    @pytest.fixture
    def config(self) -> AdaptiveTimeoutConfig:
        return AdaptiveTimeoutConfig(
            enabled=True,
            min_timeout_sec=60.0,
            max_timeout_sec=600.0,
            default_timeout_sec=300.0,
            min_samples=10,
            safety_margin=1.3,
        )

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def service(self, config: AdaptiveTimeoutConfig, mock_db: MagicMock) -> AdaptiveTimeoutService:
        return AdaptiveTimeoutService(config=config, repository=mock_db)

    @pytest.mark.asyncio
    async def test_disabled_returns_default(self, mock_db: MagicMock) -> None:
        config = AdaptiveTimeoutConfig(enabled=False, default_timeout_sec=300.0)
        service = AdaptiveTimeoutService(config=config, repository=mock_db)

        estimate = await service.get_timeout(url="https://example.com")
        assert estimate.timeout_sec == 300.0
        assert estimate.source == "disabled"
        assert estimate.confidence == 1.0

    @pytest.mark.asyncio
    async def test_no_data_returns_default(self, service: AdaptiveTimeoutService) -> None:
        # Mock the repository to return empty stats
        service._repo.async_get_combined_url_processing_stats = AsyncMock(
            return_value=LatencyStats(p50_ms=None, p95_ms=None, sample_count=0)
        )
        service._repo.async_get_domain_latency_stats = AsyncMock(
            return_value=LatencyStats(p50_ms=None, p95_ms=None, sample_count=0)
        )
        service._repo.async_get_model_latency_stats = AsyncMock(
            return_value=LatencyStats(p50_ms=None, p95_ms=None, sample_count=0)
        )
        service._repo.async_get_global_latency_stats = AsyncMock(
            return_value=LatencyStats(p50_ms=None, p95_ms=None, sample_count=0)
        )

        estimate = await service.get_timeout(url="https://example.com")
        assert estimate.source == "default"
        assert estimate.timeout_sec == 300.0

    @pytest.mark.asyncio
    async def test_domain_stats_used_when_available(self, service: AdaptiveTimeoutService) -> None:
        # Mock combined to return insufficient data
        service._repo.async_get_combined_url_processing_stats = AsyncMock(
            return_value=LatencyStats(p50_ms=None, p95_ms=None, sample_count=0)
        )
        # Mock domain stats to return sufficient data
        service._repo.async_get_domain_latency_stats = AsyncMock(
            return_value=LatencyStats(p50_ms=100000.0, p95_ms=200000.0, sample_count=20)
        )

        estimate = await service.get_timeout(url="https://example.com")
        assert estimate.source == "domain"
        # P95 = 200000ms = 200s, * 1.3 safety margin = 260s
        assert estimate.timeout_sec == pytest.approx(260.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_timeout_clamped_to_max(self, service: AdaptiveTimeoutService) -> None:
        # Mock with very high latency that would exceed max
        service._repo.async_get_combined_url_processing_stats = AsyncMock(
            return_value=LatencyStats(p50_ms=None, p95_ms=None, sample_count=0)
        )
        service._repo.async_get_domain_latency_stats = AsyncMock(
            return_value=LatencyStats(
                p50_ms=500000.0, p95_ms=1000000.0, sample_count=20
            )  # 1000s P95
        )

        estimate = await service.get_timeout(url="https://example.com")
        assert estimate.timeout_sec == 600.0  # Clamped to max

    @pytest.mark.asyncio
    async def test_timeout_clamped_to_min(self, service: AdaptiveTimeoutService) -> None:
        # Mock with very low latency that would be below min
        service._repo.async_get_combined_url_processing_stats = AsyncMock(
            return_value=LatencyStats(p50_ms=None, p95_ms=None, sample_count=0)
        )
        service._repo.async_get_domain_latency_stats = AsyncMock(
            return_value=LatencyStats(p50_ms=1000.0, p95_ms=5000.0, sample_count=20)  # 5s P95
        )

        estimate = await service.get_timeout(url="https://example.com")
        assert estimate.timeout_sec == 60.0  # Clamped to min

    @pytest.mark.asyncio
    async def test_content_length_estimation(self, service: AdaptiveTimeoutService) -> None:
        # Mock all stats to return insufficient data
        service._repo.async_get_combined_url_processing_stats = AsyncMock(
            return_value=LatencyStats(p50_ms=None, p95_ms=None, sample_count=0)
        )
        service._repo.async_get_domain_latency_stats = AsyncMock(
            return_value=LatencyStats(p50_ms=None, p95_ms=None, sample_count=0)
        )
        service._repo.async_get_model_latency_stats = AsyncMock(
            return_value=LatencyStats(p50_ms=None, p95_ms=None, sample_count=0)
        )
        service._repo.async_get_global_latency_stats = AsyncMock(
            return_value=LatencyStats(p50_ms=None, p95_ms=None, sample_count=0)
        )

        # Test with 50k characters
        estimate = await service.get_timeout(content_length=50000)
        assert estimate.source == "content"
        # Base 60s + (50000/10000) * 5s = 60 + 25 = 85s
        assert estimate.timeout_sec == pytest.approx(85.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_cache_warming(self, service: AdaptiveTimeoutService) -> None:
        # Mock global stats
        service._repo.async_get_global_latency_stats = AsyncMock(
            return_value=LatencyStats(p50_ms=100000.0, p95_ms=200000.0, sample_count=50)
        )

        await service.warm_cache()

        assert service._initialized is True
        # Verify cache was populated
        cached = await service._cache.get_global()
        assert cached is not None
        assert cached.sample_count == 50

    @pytest.mark.asyncio
    async def test_get_stats_summary(self, service: AdaptiveTimeoutService) -> None:
        summary = service.get_stats_summary()

        assert "enabled" in summary
        assert "initialized" in summary
        assert "cache_ttl_sec" in summary
        assert "min_timeout_sec" in summary
        assert "max_timeout_sec" in summary
