"""Search filter utilities for filtering search results."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.core.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class SearchFilters:
    """Filters for search queries.

    All filters are optional. Results matching ALL specified filters will be returned.
    """

    # Date filters
    date_from: dt.datetime | None = None
    date_to: dt.datetime | None = None

    # Source filters
    sources: list[str] | None = None  # List of allowed sources/domains
    exclude_sources: list[str] | None = None  # List of excluded sources/domains

    # Language filter
    languages: list[str] | None = None  # List of allowed language codes (e.g., ['en', 'ru'])

    def matches(self, result: Any) -> bool:
        """Check if a result matches all filters.

        Args:
            result: Search result to check (TopicArticle or VectorSearchResult)

        Returns:
            True if result matches all filters, False otherwise
        """
        # Date filter
        if self.date_from or self.date_to:
            published_at = getattr(result, "published_at", None)
            if published_at:
                # Try to parse published_at string to datetime
                result_date = self._parse_date(published_at)
                if result_date:
                    date_from = self._align_timezone(self.date_from, result_date)
                    date_to = self._align_timezone(self.date_to, result_date)
                    if date_from and result_date < date_from:
                        return False
                    if date_to and result_date > date_to:
                        return False
                else:
                    # If we can't parse the date and filters are set, exclude the result
                    return False
            else:
                # No published date available, exclude if date filters are set
                return False

        # Source filter (include)
        if self.sources:
            source = getattr(result, "source", None)
            if not source:
                return False
            # Case-insensitive matching
            source_lower = source.lower()
            if not any(s.lower() in source_lower for s in self.sources):
                return False

        # Source filter (exclude)
        if self.exclude_sources:
            source = getattr(result, "source", None)
            if source:
                source_lower = source.lower()
                if any(s.lower() in source_lower for s in self.exclude_sources):
                    return False

        # Language filter
        if self.languages:
            # Check if result has language attribute (e.g. VectorSearchResult)
            # TopicArticle currently does not have language, so we skip filtering for it
            language = getattr(result, "language", None)
            if language:
                # Case-insensitive match
                lang_lower = str(language).lower()
                if not any(lang.lower() == lang_lower for lang in self.languages):
                    return False

        return True

    @staticmethod
    def _align_timezone(
        boundary: dt.datetime | None, result_date: dt.datetime
    ) -> dt.datetime | None:
        if boundary is None:
            return None
        if result_date.tzinfo is not None and boundary.tzinfo is None:
            return boundary.replace(tzinfo=result_date.tzinfo)
        if result_date.tzinfo is None and boundary.tzinfo is not None:
            return boundary.replace(tzinfo=None)
        return boundary

    @staticmethod
    def _parse_date(date_str: str) -> dt.datetime | None:
        """Parse date string to datetime.

        Supports common date formats:
        - ISO 8601: 2024-01-15T10:30:00
        - Date only: 2024-01-15
        - Common formats: 15 Jan 2024, Jan 15, 2024

        Args:
            date_str: Date string to parse

        Returns:
            Datetime object or None if parsing fails
        """
        if not date_str or not isinstance(date_str, str):
            return None

        # Try ISO 8601 format first; always produce UTC-aware datetimes so that
        # comparisons with aware date_from / date_to never raise TypeError.
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                return dt.datetime.strptime(date_str.strip(), fmt).replace(tzinfo=dt.UTC)
            except ValueError:
                continue

        # Try dateutil if available (handles more formats).
        # Normalize naive results to UTC so comparisons stay consistent.
        try:
            from dateutil import parser

            result = parser.parse(date_str)
            if result.tzinfo is None:
                result = result.replace(tzinfo=dt.UTC)
            return result
        except Exception as e:
            logger.debug(
                "dateutil_parse_failed",
                extra={"date_str": date_str, "error": str(e)},
            )
            return None

    def has_filters(self) -> bool:
        """Check if any filters are set.

        Returns:
            True if at least one filter is set, False otherwise
        """
        return bool(
            self.date_from or self.date_to or self.sources or self.exclude_sources or self.languages
        )

    def __str__(self) -> str:
        """String representation of active filters."""
        parts = []
        if self.date_from:
            parts.append(f"date_from={self.date_from.date()}")
        if self.date_to:
            parts.append(f"date_to={self.date_to.date()}")
        if self.sources:
            parts.append(f"sources={','.join(self.sources)}")
        if self.exclude_sources:
            parts.append(f"exclude={','.join(self.exclude_sources)}")
        if self.languages:
            parts.append(f"lang={','.join(self.languages)}")

        return f"SearchFilters({', '.join(parts)})" if parts else "SearchFilters(none)"
