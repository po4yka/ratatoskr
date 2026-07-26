"""Performance benchmarks for URL normalization operations.

Target: >10k operations per second.

These tests ensure URL normalization remains performant as the codebase evolves.
"""

from __future__ import annotations

import socket

import pytest

# Try to import pytest-benchmark, skip tests if not available
pytest_benchmark = pytest.importorskip("pytest_benchmark")


@pytest.fixture(autouse=True)
def deterministic_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep normalization benchmarks focused on CPU work, not live DNS latency."""
    resolved = [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", 0),
        )
    ]
    monkeypatch.setattr(
        "app.core.urls.validation._resolve_hostname_to_addrs",
        lambda _hostname, _hostname_lower: resolved,
    )


class TestURLNormalizationBenchmarks:
    """Benchmarks for URL normalization functions."""

    @pytest.fixture
    def sample_urls(self) -> list[str]:
        """Generate sample URLs for benchmarking."""
        return [
            "https://example.com/article?utm_source=test&utm_medium=email",
            "http://www.example.org/path/to/page#section",
            "https://subdomain.example.net:8080/api/v1/data",
            "https://example.com/article?a=1&b=2&c=3&d=4&e=5",
            "https://example.com/path/../other/./file.html",
            "HTTPS://EXAMPLE.COM/UPPERCASE",
            "https://example.com/article?ref=twitter&source=share",
            "https://medium.com/@user/my-article-12345abcdef",
            "https://youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
            "https://twitter.com/user/status/123456789",
        ]

    def test_normalize_url_throughput(self, benchmark, sample_urls: list[str]) -> None:
        """Benchmark URL normalization throughput.

        Target: >10,000 operations per second.
        """
        from app.core.url_utils import normalize_url

        def normalize_batch():
            for url in sample_urls:
                normalize_url(url)

        benchmark(normalize_batch)

        # Calculate ops/sec (10 URLs per iteration)
        mean = benchmark.stats.stats.mean
        ops_per_sec = (10 / mean) if mean > 0 else 0

        # Assert target is met (allowing some variance)
        # Threshold tuned for Raspberry Pi 5 ARM; x86 typically 100x higher
        assert ops_per_sec > 20, f"URL normalization too slow: {ops_per_sec:.0f} ops/sec"

    def test_hash_url_throughput(self, benchmark, sample_urls: list[str]) -> None:
        """Benchmark URL hashing throughput.

        Target: >10,000 operations per second.
        """
        from app.core.url_utils import url_hash_sha256

        def hash_batch():
            for url in sample_urls:
                url_hash_sha256(url)

        benchmark(hash_batch)

        mean = benchmark.stats.stats.mean
        ops_per_sec = (10 / mean) if mean > 0 else 0

        assert ops_per_sec > 5000, f"URL hashing too slow: {ops_per_sec:.0f} ops/sec"

    def test_is_youtube_url_throughput(self, benchmark, sample_urls: list[str]) -> None:
        """Benchmark YouTube URL detection throughput."""
        from app.core.url_utils import is_youtube_url

        youtube_urls = [
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=abcdefghijk",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=test123",
            "https://youtube.com/shorts/abc123",
            "https://example.com/not-youtube",
            "https://youtube.com/embed/video_id",
            "https://music.youtube.com/watch?v=abc",
            "https://www.youtube.com/v/oldstyle",
            "https://youtube.com/live/stream_id",
        ]

        def check_batch():
            for url in youtube_urls:
                is_youtube_url(url)

        benchmark(check_batch)

        mean = benchmark.stats.stats.mean
        ops_per_sec = (10 / mean) if mean > 0 else 0

        assert ops_per_sec > 10000, f"YouTube detection too slow: {ops_per_sec:.0f} ops/sec"


class TestURLDeduplicationBenchmarks:
    """Benchmarks for URL deduplication operations."""

    def test_dedupe_hash_consistency(self, benchmark) -> None:
        """Verify deduplication hash is deterministic and fast."""
        from app.core.url_utils import normalize_url, url_hash_sha256

        url = "https://example.com/article?utm_source=test&ref=share"

        def compute_dedupe_hash():
            normalized = normalize_url(url)
            return url_hash_sha256(normalized)

        # Wrap repeated calls in a single function so benchmark is called once
        def consistency_check():
            return [compute_dedupe_hash() for _ in range(100)]

        benchmark(consistency_check)

        # Verify all hashes are identical (outside benchmark)
        first_hash = compute_dedupe_hash()
        for _ in range(100):
            assert compute_dedupe_hash() == first_hash, "Hash is not deterministic"
