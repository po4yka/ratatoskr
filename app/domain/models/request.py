"""Request domain model.

This module defines the Request entity, which represents a user's request
to process content (URL, forward, etc.).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class RequestType(StrEnum):
    """Types of requests that can be processed."""

    URL = "url"
    FORWARD = "forward"
    TEXT = "text"
    # Free-form interactive browser-agent task initiated via `/browse`.
    # Lifecycle drains the same status enum (pending -> crawling -> completed)
    # but the body lives in `webwright_runs` rather than `summaries`.
    WEBWRIGHT = "webwright"
    UNKNOWN = "unknown"


class RequestStatus(StrEnum):
    """Status of a request in its lifecycle."""

    PENDING = "pending"
    CRAWLING = "crawling"
    SUMMARIZING = "summarizing"
    COMPLETED = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"
    # Terminal status for rows created by the x_bookmarks bookmark ingestor:
    # bookmark metadata is mirrored from `ft sync` without invoking the
    # scraper chain or summarizer; the row never enters the URL processor.
    X_IMPORTED = "x_imported"


@dataclass
class Request:
    """Domain model for content processing request.

    Rich domain model that encapsulates request data and business logic.
    This is framework-agnostic and contains no infrastructure concerns.
    """

    user_id: int
    chat_id: int
    request_type: RequestType
    status: RequestStatus = RequestStatus.PENDING
    input_url: str | None = None
    normalized_url: str | None = None
    dedupe_hash: str | None = None
    correlation_id: str | None = None
    input_message_id: int | None = None
    fwd_from_chat_id: int | None = None
    fwd_from_msg_id: int | None = None
    lang_detected: str | None = None
    content_text: str | None = None
    route_version: int = 1
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def has_url(self) -> bool:
        """Return True if input_url or normalized_url is set."""
        return bool(self.input_url or self.normalized_url)

    def get_url(self) -> str | None:
        """Return normalized URL if available, otherwise input URL."""
        return self.normalized_url or self.input_url

    def __str__(self) -> str:
        """String representation of the request."""
        return f"Request(id={self.id}, type={self.request_type.value}, status={self.status.value})"

    def __repr__(self) -> str:
        """Detailed representation of the request."""
        url_info = f", url={self.get_url()[:50]}..." if self.has_url() else ""
        return (
            f"Request(id={self.id}, user_id={self.user_id}, "
            f"type={self.request_type.value}, status={self.status.value}{url_info})"
        )
