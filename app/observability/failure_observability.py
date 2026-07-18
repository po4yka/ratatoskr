"""Helpers for normalized extraction failure observability.

This module standardizes failure snapshots persisted on `requests.error_context_json`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import redact_for_logging, redact_url_for_logging
from app.core.time_utils import UTC
from app.observability.metrics import record_extraction_failure

if TYPE_CHECKING:
    import logging


REASON_RESOLVE_FAILED = "RESOLVE_FAILED"
REASON_DNS_RESOLUTION_FAILED = "DNS_RESOLUTION_FAILED"
REASON_NOT_ARTICLE = "NOT_ARTICLE"
REASON_FIRECRAWL_ERROR = "FIRECRAWL_ERROR"
REASON_FIRECRAWL_LOW_VALUE = "FIRECRAWL_LOW_VALUE"
REASON_SCRAPER_CHAIN_EXHAUSTED = "SCRAPER_CHAIN_EXHAUSTED"
REASON_PLAYWRIGHT_EMPTY_CONTENT = "PLAYWRIGHT_EMPTY_CONTENT"
REASON_PLAYWRIGHT_UI_OR_LOGIN = "PLAYWRIGHT_UI_OR_LOGIN"
REASON_DIRECT_FETCH_FAILED = "DIRECT_FETCH_FAILED"
REASON_EXTRACTION_EMPTY_OUTPUT = "EXTRACTION_EMPTY_OUTPUT"
REASON_UNKNOWN_EXTRACTION_FAILURE = "UNKNOWN_EXTRACTION_FAILURE"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sanitize_url_for_logs(url: str | None, *, max_length: int = 400) -> str | None:
    """Sanitize URLs for logs by removing private path, query, fragment, and credentials."""
    if not url:
        return None
    return str(redact_url_for_logging(url, max_length=max_length))


def truncate_for_logs(value: str | None, *, max_length: int = 500) -> str | None:
    if not value:
        return value
    text = str(redact_for_logging(str(value)))
    if len(text) <= max_length:
        return text
    return text[: max_length - 15] + "... [truncated]"


@dataclass(frozen=True)
class ExtractionFailureSnapshot:
    """Normalized failure payload persisted in requests.error_context_json."""

    failure_id: str
    timestamp: str
    correlation_id: str | None
    request_id: int
    pipeline: str
    stage: str
    component: str
    reason_code: str
    error_type: str
    error_message: str
    retryable: bool
    attempt: int
    max_attempts: int
    http_status: int | None = None
    provider_error_code: str | None = None
    latency_ms: int | None = None
    source_url: str | None = None
    resolved_url: str | None = None
    canonical_url: str | None = None
    article_id: str | None = None
    quality_reason: str | None = None
    content_signals: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "failure_id": self.failure_id,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
            "request_id": self.request_id,
            "pipeline": self.pipeline,
            "stage": self.stage,
            "component": self.component,
            "reason_code": self.reason_code,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
        }
        optional = {
            "http_status": self.http_status,
            "provider_error_code": self.provider_error_code,
            "latency_ms": self.latency_ms,
            "source_url": self.source_url,
            "resolved_url": self.resolved_url,
            "canonical_url": self.canonical_url,
            "article_id": self.article_id,
            "quality_reason": self.quality_reason,
            "content_signals": self.content_signals,
        }
        for key, value in optional.items():
            if value is not None:
                payload[key] = value
        return payload


def build_failure_snapshot(
    *,
    request_id: int,
    correlation_id: str | None,
    stage: str,
    component: str,
    reason_code: str,
    error: BaseException | str,
    retryable: bool,
    attempt: int = 1,
    max_attempts: int = 1,
    http_status: int | None = None,
    provider_error_code: str | None = None,
    latency_ms: int | None = None,
    source_url: str | None = None,
    resolved_url: str | None = None,
    canonical_url: str | None = None,
    article_id: str | None = None,
    quality_reason: str | None = None,
    content_signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a normalized, sanitized extraction failure snapshot."""
    if isinstance(error, BaseException):
        error_type = type(error).__name__
        error_message = str(error)
    else:
        error_type = "Error"
        error_message = str(error)

    snapshot = ExtractionFailureSnapshot(
        failure_id=uuid.uuid4().hex[:16],
        timestamp=_utc_now_iso(),
        correlation_id=correlation_id,
        request_id=request_id,
        pipeline="url_extraction",
        stage=stage,
        component=component,
        reason_code=reason_code,
        error_type=error_type,
        error_message=truncate_for_logs(error_message, max_length=500) or "unknown_error",
        retryable=retryable,
        attempt=max(1, int(attempt)),
        max_attempts=max(1, int(max_attempts)),
        http_status=http_status,
        provider_error_code=provider_error_code,
        latency_ms=latency_ms,
        source_url=sanitize_url_for_logs(source_url),
        resolved_url=sanitize_url_for_logs(resolved_url),
        canonical_url=sanitize_url_for_logs(canonical_url),
        article_id=truncate_for_logs(article_id, max_length=128),
        quality_reason=truncate_for_logs(quality_reason, max_length=128),
        content_signals=content_signals,
    )
    return snapshot.to_dict()


def log_failure_snapshot(
    logger: logging.Logger,
    *,
    event: str = "extraction_failure_snapshot",
    snapshot: dict[str, Any],
) -> None:
    """Emit standardized failure snapshot logs."""
    logger.error(event, extra=snapshot)


async def persist_request_failure(
    *,
    request_repo: Any,
    logger: logging.Logger,
    request_id: int,
    correlation_id: str | None,
    stage: str,
    component: str,
    reason_code: str,
    error: BaseException | str,
    retryable: bool,
    attempt: int = 1,
    max_attempts: int = 1,
    processing_time_ms: int | None = None,
    http_status: int | None = None,
    provider_error_code: str | None = None,
    latency_ms: int | None = None,
    source_url: str | None = None,
    resolved_url: str | None = None,
    canonical_url: str | None = None,
    article_id: str | None = None,
    quality_reason: str | None = None,
    content_signals: dict[str, Any] | None = None,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """Persist a normalized request failure snapshot and emit metrics/logs."""
    snapshot = build_failure_snapshot(
        request_id=request_id,
        correlation_id=correlation_id,
        stage=stage,
        component=component,
        reason_code=reason_code,
        error=error,
        retryable=retryable,
        attempt=attempt,
        max_attempts=max_attempts,
        http_status=http_status,
        provider_error_code=provider_error_code,
        latency_ms=latency_ms,
        source_url=source_url,
        resolved_url=resolved_url,
        canonical_url=canonical_url,
        article_id=article_id,
        quality_reason=quality_reason,
        content_signals=content_signals,
    )

    record_extraction_failure(
        stage=stage,
        component=component,
        reason_code=reason_code,
        retryable=retryable,
    )
    log_failure_snapshot(logger, snapshot=snapshot)

    try:
        await request_repo.async_update_request_error(
            request_id,
            "error",
            error_type=str(snapshot.get("reason_code") or snapshot.get("error_type") or ""),
            error_message=str(snapshot.get("error_message") or ""),
            processing_time_ms=processing_time_ms,
            error_context_json=snapshot,
        )
    except Exception as exc:
        logger.warning(
            "request_failure_snapshot_persist_failed",
            extra={
                "request_id": request_id,
                "correlation_id": correlation_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        if raise_on_error:
            raise

    return snapshot


__all__ = [
    "REASON_DIRECT_FETCH_FAILED",
    "REASON_DNS_RESOLUTION_FAILED",
    "REASON_EXTRACTION_EMPTY_OUTPUT",
    "REASON_FIRECRAWL_ERROR",
    "REASON_FIRECRAWL_LOW_VALUE",
    "REASON_NOT_ARTICLE",
    "REASON_PLAYWRIGHT_EMPTY_CONTENT",
    "REASON_PLAYWRIGHT_UI_OR_LOGIN",
    "REASON_RESOLVE_FAILED",
    "REASON_SCRAPER_CHAIN_EXHAUSTED",
    "REASON_UNKNOWN_EXTRACTION_FAILURE",
    "build_failure_snapshot",
    "log_failure_snapshot",
    "persist_request_failure",
    "sanitize_url_for_logs",
    "truncate_for_logs",
]
