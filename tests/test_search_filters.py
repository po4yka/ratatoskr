from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from app.infrastructure.search.search_filters import SearchFilters


def _result(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def test_matches_all_active_filters() -> None:
    filters = SearchFilters(
        date_from=dt.datetime(2024, 1, 10),
        date_to=dt.datetime(2024, 1, 20),
        sources=["example"],
        exclude_sources=["spam"],
        languages=["en"],
    )

    result = _result(
        published_at="2024-01-15T10:30:00",
        source="https://example.com/article",
        language="EN",
    )

    assert filters.matches(result) is True


def test_matches_rejects_missing_or_unparseable_dates_when_date_filters_enabled() -> None:
    filters = SearchFilters(date_from=dt.datetime(2024, 1, 1))

    assert filters.matches(_result(source="example.com")) is False
    assert filters.matches(_result(published_at="not-a-date", source="example.com")) is False


def test_matches_applies_include_and_exclude_source_filters_case_insensitively() -> None:
    filters = SearchFilters(sources=["news.example"], exclude_sources=["blocked"])

    assert filters.matches(_result(source="https://NEWS.EXAMPLE.com/story")) is True
    assert filters.matches(_result(source="https://other.example.com/story")) is False
    assert filters.matches(_result(source="https://blocked.example.com/story")) is False


def test_language_filter_is_only_applied_when_result_has_language() -> None:
    filters = SearchFilters(languages=["ru"])

    assert filters.matches(_result(source="example.com")) is True
    assert filters.matches(_result(source="example.com", language="EN")) is False


def test_parse_date_supports_common_formats_and_invalid_values() -> None:
    assert SearchFilters._parse_date("2024-01-15").date() == dt.date(2024, 1, 15)
    assert SearchFilters._parse_date("2024-01-15T10:30:00Z") == dt.datetime(
        2024, 1, 15, 10, 30, tzinfo=dt.UTC
    )
    assert SearchFilters._parse_date("15 Jan 2024").date() == dt.date(2024, 1, 15)
    assert SearchFilters._parse_date("") is None
    assert SearchFilters._parse_date(None) is None


def test_has_filters_and_string_representation_reflect_active_filters() -> None:
    empty = SearchFilters()
    assert empty.has_filters() is False
    assert str(empty) == "SearchFilters(none)"

    active = SearchFilters(
        date_from=dt.datetime(2024, 1, 1),
        date_to=dt.datetime(2024, 1, 31),
        sources=["example.com"],
        exclude_sources=["spam.com"],
        languages=["en", "ru"],
    )

    assert active.has_filters() is True
    assert str(active) == (
        "SearchFilters(date_from=2024-01-01, date_to=2024-01-31, "
        "sources=example.com, exclude=spam.com, lang=en,ru)"
    )
