"""Debug logging utilities for Firecrawl client.

This module provides logging helpers for:
- Safe audit callback invocation
- Request/response payload logging
- Debug payload inspection
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import bounded_debug_preview, get_logger, redact_for_logging

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable


class PayloadLogger:
    """Handles logging and audit operations for Firecrawl client."""

    def __init__(
        self,
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
        debug_payloads: bool = False,
        log_truncate_length: int = 1000,
        logger: logging.Logger | None = None,
    ) -> None:
        self._audit = audit
        self._debug_payloads = debug_payloads
        self._log_truncate_length = log_truncate_length
        self._logger = logger or get_logger(__name__)

    def audit_safe(self, level: str, event: str, details: dict[str, Any]) -> None:
        """Safely invoke audit callback, suppressing any exceptions.

        Args:
            level: Log level (INFO, WARN, ERROR)
            event: Event name for categorization
            details: Event details dictionary
        """
        if not self._audit:
            return
        with contextlib.suppress(Exception):
            self._audit(level, event, details)

    def log_scrape_attempt(
        self,
        *,
        attempt: int,
        url: str,
        mobile: bool,
        pdf: bool,
        request_id: int | None,
    ) -> None:
        """Log a scrape attempt.

        Args:
            attempt: Current attempt number (0-indexed)
            url: URL being scraped
            mobile: Whether mobile mode is enabled
            pdf: Whether PDF parsing is enabled
            request_id: Optional request identifier
        """
        self.audit_safe(
            "INFO",
            "firecrawl_attempt",
            {
                "attempt": attempt,
                "url": redact_for_logging(url, key="url"),
                "mobile": mobile,
                "pdf": pdf,
                "request_id": request_id,
            },
        )
        self._logger.debug(
            "firecrawl_request",
            extra={
                "attempt": attempt,
                "url": redact_for_logging(url, key="url"),
                "mobile": mobile,
                "pdf": pdf,
                "request_id": request_id,
            },
        )

    def log_request_payload(self, json_body: dict[str, Any]) -> None:
        """Log request payload if debug_payloads is enabled.

        Args:
            json_body: Request JSON body to log
        """
        if self._debug_payloads:
            self._logger.debug(
                "firecrawl_request_payload",
                extra={
                    "debug_payload_preview": bounded_debug_preview(
                        redact_for_logging(json_body, allow_debug_content=True),
                        max_chars=self._log_truncate_length,
                    )
                },
            )

    def log_response(
        self,
        *,
        status_code: int,
        latency_ms: int,
        request_id: int | None,
    ) -> None:
        """Log response metadata.

        Args:
            status_code: HTTP status code
            latency_ms: Response latency in milliseconds
            request_id: Optional request identifier
        """
        self._logger.debug(
            "firecrawl_response",
            extra={
                "status": status_code,
                "latency_ms": latency_ms,
                "request_id": request_id,
            },
        )

    def log_response_payload(self, data: dict[str, Any]) -> None:
        """Log response payload preview if debug_payloads is enabled.

        Args:
            data: Response data dictionary
        """
        if self._debug_payloads:
            preview = {
                "keys": list(data.keys()) if isinstance(data, dict) else None,
                "markdown_len": (
                    len(data.get("markdown") or "") if isinstance(data, dict) else None
                ),
            }
            self._logger.debug("firecrawl_response_payload", extra={"preview": preview})

    def log_response_debug(
        self,
        data: dict[str, Any],
        correlation_id: str | None,
    ) -> None:
        """Log detailed response debug information.

        Args:
            data: Response data dictionary
            correlation_id: Firecrawl correlation ID
        """
        response_error = data.get("error")
        markdown_len = len(data.get("markdown") or "") if isinstance(data, dict) else None
        html_len = len(data.get("html") or "") if isinstance(data, dict) else None
        data_items = None
        if isinstance(data.get("data"), list):
            data_items = len(data["data"])
        elif isinstance(data.get("data"), dict):
            data_items = 1

        self._logger.debug(
            "firecrawl_response_debug",
            extra={
                "status_code": data.get("status_code"),
                "response_keys": list(data.keys()) if isinstance(data, dict) else None,
                "error_field": response_error,
                "error_type": type(response_error).__name__,
                "success_field": data.get("success"),
                "markdown_len": markdown_len,
                "html_len": html_len,
                "data_items": data_items,
                "correlation_id": correlation_id,
            },
        )

    def log_error(
        self,
        *,
        attempt: int,
        status: int | None,
        error: str,
        pdf: bool,
        request_id: int | None,
    ) -> None:
        """Log an error event.

        Args:
            attempt: Current attempt number
            status: HTTP status code (if available)
            error: Error message
            pdf: Whether PDF parsing was enabled
            request_id: Optional request identifier
        """
        self.audit_safe(
            "ERROR",
            "firecrawl_error",
            {
                "attempt": attempt,
                "status": status,
                "error": error,
                "pdf": pdf,
                "request_id": request_id,
            },
        )
        self._logger.error(
            "firecrawl_error",
            extra={"status": status, "error": error},
        )

    def log_success(
        self,
        *,
        attempt: int,
        status: int | None,
        latency_ms: int,
        pdf: bool,
        request_id: int | None,
    ) -> None:
        """Log a success event.

        Args:
            attempt: Current attempt number
            status: HTTP status code
            latency_ms: Response latency in milliseconds
            pdf: Whether PDF parsing was enabled
            request_id: Optional request identifier
        """
        self.audit_safe(
            "INFO",
            "firecrawl_success",
            {
                "attempt": attempt,
                "status": status,
                "latency_ms": latency_ms,
                "pdf": pdf,
                "request_id": request_id,
            },
        )

    def log_exhausted(
        self,
        *,
        attempts: int,
        error: str | None,
        request_id: int | None,
    ) -> None:
        """Log when all retry attempts are exhausted.

        Args:
            attempts: Total number of attempts made
            error: Last error message
            request_id: Optional request identifier
        """
        self.audit_safe(
            "ERROR",
            "firecrawl_exhausted",
            {"attempts": attempts, "error": error, "request_id": request_id},
        )

    def log_rate_limit(
        self,
        *,
        status: int,
        retry_after: int,
        attempt: int,
    ) -> None:
        """Log a rate limit event.

        Args:
            status: HTTP status code (429)
            retry_after: Retry-after value in seconds
            attempt: Current attempt number
        """
        self._logger.warning(
            "firecrawl_rate_limit",
            extra={
                "status": status,
                "retry_after": retry_after,
                "attempt": attempt,
            },
        )

    def log_response_too_large(
        self,
        *,
        error: str,
        url: str,
        max_size_mb: float,
    ) -> None:
        """Log when response exceeds size limit.

        Args:
            error: Error message
            url: URL being scraped
            max_size_mb: Maximum allowed size in MB
        """
        self._logger.error(
            "firecrawl_response_too_large",
            extra={
                "error": error,
                "url": url,
                "max_size_mb": max_size_mb,
            },
        )

    def log_invalid_json(self, error: str, status_code: int) -> None:
        """Log when response contains invalid JSON.

        Args:
            error: Error message from JSON decode
            status_code: HTTP status code
        """
        self._logger.exception(
            "firecrawl_invalid_json",
            extra={"error": error, "status": status_code},
        )

    def log_exception(self, exc: BaseException, attempt: int) -> None:
        """Log an exception during request with compact error triple.

        At DEBUG level the full traceback is included; at WARNING level only
        error_class / error_message / top_frame are emitted to keep log volume
        manageable.

        Args:
            exc: The caught exception object
            attempt: Current attempt number
        """
        import traceback

        error_class = type(exc).__name__
        error_message = str(exc) or "<empty>"
        top_frame = ""
        tb = exc.__traceback__
        if tb is not None:
            frames = traceback.extract_tb(tb)
            if frames:
                last = frames[-1]
                top_frame = f"{last.filename}:{last.lineno}"

        extra = {
            "error_class": error_class,
            "error_message": error_message,
            "top_frame": top_frame,
            "attempt": attempt,
        }
        if self._logger.isEnabledFor(10):  # logging.DEBUG
            self._logger.exception("firecrawl_exception", extra=extra)
        else:
            self._logger.warning("firecrawl_exception", extra=extra)

    def log_search_request(
        self,
        *,
        query: str,
        limit: int,
        request_id: int | None,
    ) -> None:
        """Log a search request.

        Args:
            query: Search query
            limit: Number of results requested
            request_id: Optional request identifier
        """
        self.audit_safe(
            "INFO",
            "firecrawl_search_request",
            {"query": query, "limit": limit, "request_id": request_id},
        )
        self._logger.debug(
            "firecrawl_search_request",
            extra={"query": query, "limit": limit, "request_id": request_id},
        )

    def log_search_response(
        self,
        *,
        status: str,
        http_status: int,
        result_count: int,
        query: str,
        latency_ms: int,
    ) -> None:
        """Log a search response.

        Args:
            status: Response status (success/error)
            http_status: HTTP status code
            result_count: Number of results returned
            query: Original search query
            latency_ms: Response latency in milliseconds
        """
        self.audit_safe(
            "INFO" if status == "success" else "ERROR",
            "firecrawl_search_response",
            {
                "status": status,
                "http_status": http_status,
                "result_count": result_count,
                "query": query,
            },
        )
        self._logger.debug(
            "firecrawl_search_response",
            extra={
                "status": status,
                "http_status": http_status,
                "results": result_count,
                "latency_ms": latency_ms,
            },
        )

    def log_search_size_error(self, error: str, query: str) -> None:
        """Log when search response exceeds size limit.

        Args:
            error: Error message
            query: Search query
        """
        self.audit_safe(
            "ERROR",
            "firecrawl_search_response_too_large",
            {"error": error, "query": query},
        )

    def log_search_http_error(self, error: str, query: str) -> None:
        """Log HTTP error during search.

        Args:
            error: Error message
            query: Search query
        """
        self.audit_safe(
            "ERROR",
            "firecrawl_search_http_error",
            {"error": error, "query": query},
        )

    def log_search_invalid_json(self, status_code: int, error: str) -> None:
        """Log when search response contains invalid JSON.

        Args:
            status_code: HTTP status code
            error: Error message
        """
        self.audit_safe(
            "ERROR",
            "firecrawl_search_invalid_json",
            {"status": status_code, "error": error},
        )
