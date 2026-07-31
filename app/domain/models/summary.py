"""Summary domain model.

This module defines the Summary entity, which represents a processed
content summary with its metadata and insights.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from app.core.time_utils import coerce_datetime

# Canonical list of fields that every valid summary must contain.
# Import this constant instead of hardcoding the field names elsewhere.
REQUIRED_SUMMARY_FIELDS: list[str] = ["tldr", "summary_250", "key_ideas"]


@dataclass
class Summary:
    """Domain model for content summary.

    Rich domain model that encapsulates summary data and business logic.
    This is framework-agnostic and contains no infrastructure concerns.
    """

    request_id: int
    content: dict[str, Any]
    language: str
    version: int = 1
    is_read: bool = False
    insights: dict[str, Any] | None = None
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def mark_as_read(self) -> None:
        """Mark this summary as read.

        Raises:
            ValueError: If summary is already marked as read.

        """
        if self.is_read:
            msg = "Summary is already marked as read"
            raise ValueError(msg)
        self.is_read = True

    def mark_as_unread(self) -> None:
        """Mark this summary as unread.

        Raises:
            ValueError: If summary is already marked as unread.

        """
        if not self.is_read:
            msg = "Summary is already marked as unread"
            raise ValueError(msg)
        self.is_read = False

    def validate_content(self) -> bool:
        """Return True if content has all required fields with non-empty values."""
        return all(
            field in self.content
            and self.content[field]
            and (not isinstance(self.content[field], str) or self.content[field].strip())
            for field in REQUIRED_SUMMARY_FIELDS
        )

    def has_insights(self) -> bool:
        """Return True if insights exist and are not empty."""
        return self.insights is not None and bool(self.insights)

    def get_reading_time_minutes(self) -> int:
        """Return estimated reading time in minutes, or 0 if not available."""
        return cast("int", self.content.get("estimated_reading_time_min", 0))

    def get_tldr(self) -> str:
        """Return TL;DR text, or empty string if not available."""
        return cast("str", self.content.get("tldr", ""))

    def get_summary_250(self) -> str:
        """Return 250-character summary text, or empty string if not available."""
        return cast("str", self.content.get("summary_250", ""))

    def get_summary_1000(self) -> str:
        """Return 1000-character summary text, or empty string if not available."""
        return cast("str", self.content.get("summary_1000", ""))

    def get_key_ideas(self) -> list[str]:
        """Return list of key ideas, or empty list if not available."""
        key_ideas = self.content.get("key_ideas", [])
        return key_ideas if isinstance(key_ideas, list) else []

    def get_topic_tags(self) -> list[str]:
        """Return list of topic tags, or empty list if not available."""
        tags = self.content.get("topic_tags", [])
        return tags if isinstance(tags, list) else []

    def has_minimum_content(self) -> bool:
        """Return True if at least one summary field (tldr, summary_250, summary_1000) exists."""
        return bool(self.get_tldr() or self.get_summary_250() or self.get_summary_1000())

    def __str__(self) -> str:
        """String representation of the summary."""
        status = "read" if self.is_read else "unread"
        return (
            f"Summary(id={self.id}, request_id={self.request_id}, lang={self.language}, {status})"
        )

    def __repr__(self) -> str:
        """Detailed representation of the summary."""
        return (
            f"Summary(id={self.id}, request_id={self.request_id}, "
            f"lang={self.language}, version={self.version}, "
            f"is_read={self.is_read}, has_insights={self.has_insights()})"
        )


def summary_from_dict(db_summary: dict[str, Any]) -> Summary:
    """Convert a persistence dictionary into a Summary domain model."""
    return Summary(
        id=db_summary.get("id"),
        request_id=db_summary.get("request_id") or db_summary.get("request"),
        content=db_summary.get("json_payload"),
        language=db_summary.get("lang"),
        version=db_summary.get("version", 1),
        is_read=db_summary.get("is_read", False),
        insights=db_summary.get("insights_json"),
        created_at=coerce_datetime(db_summary.get("created_at")),
    )
