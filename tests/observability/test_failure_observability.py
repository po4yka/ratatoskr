from __future__ import annotations

from app.observability.failure_observability import (
    REASON_FIRECRAWL_ERROR,
    build_failure_snapshot,
)


def test_build_failure_snapshot_contains_required_fields() -> None:
    snapshot = build_failure_snapshot(
        request_id=123,
        correlation_id="cid-123",
        stage="extraction",
        component="firecrawl",
        reason_code=REASON_FIRECRAWL_ERROR,
        error=ValueError("firecrawl failed hard"),
        retryable=True,
        attempt=2,
        max_attempts=3,
        source_url="https://example.com/a/path?token=secret#frag",
    )

    assert snapshot["request_id"] == 123
    assert snapshot["correlation_id"] == "cid-123"
    assert snapshot["pipeline"] == "url_extraction"
    assert snapshot["stage"] == "extraction"
    assert snapshot["component"] == "firecrawl"
    assert snapshot["reason_code"] == REASON_FIRECRAWL_ERROR
    assert snapshot["retryable"] is True
    assert snapshot["attempt"] == 2
    assert snapshot["max_attempts"] == 3
    assert "failure_id" in snapshot
    assert "timestamp" in snapshot


def test_build_failure_snapshot_redacts_url_query_and_fragment() -> None:
    snapshot = build_failure_snapshot(
        request_id=1,
        correlation_id="cid-1",
        stage="extraction",
        component="firecrawl",
        reason_code=REASON_FIRECRAWL_ERROR,
        error="failed",
        retryable=True,
        source_url="https://example.com/a/path?token=secret&x=1#frag",
    )
    assert snapshot["source_url"] == "https://example.com/[redacted]?token=%5BREDACTED%5D"
