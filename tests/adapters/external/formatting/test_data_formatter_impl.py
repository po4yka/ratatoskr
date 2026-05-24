from __future__ import annotations

import math

from app.adapters.external.formatting.data_formatter import (
    DataFormatterImpl,
    normalize_metric_names,
)


def test_data_formatter_formats_bytes_and_metric_values() -> None:
    formatter = DataFormatterImpl()

    assert formatter.format_bytes(0) == "0 B"
    assert formatter.format_bytes(1023) == "1023 B"
    assert formatter.format_bytes(1024) == "1.0 KB"
    assert formatter.format_bytes(1024 * 1024) == "1.0 MB"
    assert formatter.format_bytes(1024**4 * 3) == "3.0 TB"

    assert formatter.format_metric_value(None) is None
    assert formatter.format_metric_value(True) == "Yes"
    assert formatter.format_metric_value(False) == "No"
    assert formatter.format_metric_value(12) == "12"
    assert formatter.format_metric_value(12.0) == "12"
    assert formatter.format_metric_value(12.345) == "12.35"
    assert formatter.format_metric_value(12.3) == "12.3"
    assert formatter.format_metric_value(math.inf) == "inf"
    assert formatter.format_metric_value("  value  ") == "value"

    ru_formatter = DataFormatterImpl("ru")
    assert ru_formatter.format_metric_value(True) == "Да"
    assert ru_formatter.format_metric_value(False) == "Нет"


def test_data_formatter_renders_key_stats_and_readability_with_escaping() -> None:
    formatter = DataFormatterImpl()

    stats = formatter.format_key_stats(
        [
            {
                "label": "Revenue <gross>",
                "value": 12.5,
                "unit": "USD <m>",
                "source_excerpt": "Source <quote>",
            },
            {"label": "Flag", "value": True},
            {"label": "Unit only", "unit": "%"},
            {"label": "Label only"},
            "bad",
            {"value": 1},
        ]
    )
    assert stats == [
        "• Revenue &lt;gross&gt;: <code>12.5</code> USD &lt;m&gt; — Source: Source &lt;quote&gt;",
        "• Flag: <code>Yes</code>",
        "• Unit only: %",
        "• Label only",
    ]

    compact = formatter.format_key_stats_compact(
        [
            {"label": "Speed", "value": 14_000, "unit": "km/h"},
            {"label": "Unit", "unit": "points"},
        ]
    )
    assert compact == ["• Speed: <code>14000</code> km/h", "• Unit: points"]

    assert formatter.format_readability(None) is None
    assert formatter.format_readability("bad") is None
    assert (
        formatter.format_readability(
            [
                {"method": "flesch", "score": 42.25, "level": "college"},
                {"method": "gunning"},
                {"score": 10},
                "bad",
            ]
        )
        == "Flesch: Score: <code>42.25</code> • Level: College | Gunning | Score: <code>10</code>"
    )


def test_data_formatter_normalizes_metrics_and_firecrawl_options() -> None:
    formatter = DataFormatterImpl()

    raw = {
        "reading_time": 3,
        "Time_To_Read": 4,
        "complexity": "medium",
        "words": 200,
        "detected_language": "en",
        "unchanged": True,
    }
    expected = {
        "estimated_reading_time_min": 4,
        "readability_score": "medium",
        "word_count_approx": 200,
        "language": "en",
        "unchanged": True,
    }
    assert formatter.normalize_metric_names(raw) == expected
    assert normalize_metric_names(raw) == expected

    assert formatter.format_firecrawl_options(None) is None
    assert formatter.format_firecrawl_options({}) is None
    assert formatter.format_firecrawl_options({"empty": ""}) is None
    assert formatter.format_firecrawl_options(
        {
            "mobile": True,
            "formats": ["markdown", "html", "", "links", "screenshot", "raw"],
            "parsers": ("pdf", "article"),
            "waitFor": 1000,
            "onlyMainContent": False,
            "locale": " en-US ",
            "actions": ["click", "wait", "scroll", "screenshot", "extract", "ignored"],
        }
    ) == (
        "mobile=on; formats=markdown, html, links, screenshot, raw; "
        "parsers=pdf, article; waitFor=1000; onlyMainContent=off; locale=en-US; "
        "actions=click, wait, scroll, screenshot, extract"
    )
