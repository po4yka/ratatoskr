from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.adapters.external.formatting.export_formatter import ExportFormatter
from app.core.time_utils import UTC


def _formatter() -> ExportFormatter:
    return object.__new__(ExportFormatter)


def _summary_data() -> dict[str, object]:
    return {
        "metadata": {"title": "Title <Unsafe>"},
        "url": "https://example.test/article?x=1&y=2",
        "summary_250": "Short <summary>.",
        "summary_1000": "Detailed <summary>.",
        "key_ideas": ["Idea <one>", "Idea two"],
        "topic_tags": ["ai", "testing"],
        "entities": {
            "people": ["Ada <Lovelace>"],
            "organizations": ["OpenAI"],
            "locations": ["Paris"],
        },
        "key_stats": [{"label": "Users", "value": 42, "unit": "people"}],
        "estimated_reading_time_min": 5,
        "seo_keywords": ["AI tools", "Coverage", "Testing"],
        "created_at": datetime(2026, 5, 1, 12, 30, tzinfo=UTC),
    }


def test_export_formatter_generates_markdown_sections() -> None:
    formatter = _formatter()

    markdown = formatter._generate_markdown(_summary_data())

    assert "# Title <Unsafe>" in markdown
    assert "**Source:** [https://example.test/article?x=1&y=2]" in markdown
    assert "## TL;DR" in markdown
    assert "Short <summary>." in markdown
    assert "- Idea <one>" in markdown
    assert "## Entities" in markdown
    assert "**People:** Ada <Lovelace>" in markdown
    assert "- **Users:** 42 people" in markdown
    assert "**Estimated Reading Time:** ~5 minutes" in markdown
    assert "AI tools, Coverage, Testing" in markdown
    assert "*Generated on 2026-05-01 12:30 UTC by Ratatoskr*" in markdown


def test_export_formatter_generates_html_with_escaped_sections() -> None:
    formatter = _formatter()

    html = formatter._generate_html(_summary_data(), for_pdf=True)

    assert "<title>Title &lt;Unsafe&gt;</title>" in html
    assert "@page" in html
    assert "https://example.test/article?x=1&amp;y=2" in html
    assert "Short &lt;summary&gt;." in html
    assert "<li>Idea &lt;one&gt;</li>" in html
    assert '<span class="tag">ai</span>' in html
    assert "Ada &lt;Lovelace&gt;" in html
    assert "<li><strong>Users:</strong> 42 people</li>" in html
    assert "Generated on 2026-05-01 12:30 UTC by Ratatoskr" in html


def test_export_formatter_filename_and_slug_fallbacks() -> None:
    formatter = _formatter()

    assert formatter._slugify("  Hello, World_and test!!  ") == "hello-world-and-test"
    assert formatter._slugify("!!!") == "export"

    seo_filename = formatter._generate_filename({"seo_keywords": ["One", "Two Two"]}, "md")
    assert seo_filename.startswith("one-two-two-")
    assert seo_filename.endswith(".md")

    summary_filename = formatter._generate_filename(
        {"summary_250": "Fallback title words are used here"}, "html"
    )
    assert summary_filename.startswith("fallback-title-words-are-used-")
    assert summary_filename.endswith(".html")

    default_filename = formatter._generate_filename({}, "pdf")
    assert default_filename.startswith("summary-")
    assert default_filename.endswith(".pdf")


def test_export_formatter_writes_markdown_and_html_exports() -> None:
    formatter = _formatter()
    written_paths: list[str] = []

    try:
        md_path, md_name = formatter._export_markdown(_summary_data(), "cid")
        html_path, html_name = formatter._export_html(_summary_data(), "cid")
        assert md_path is not None
        assert html_path is not None
        written_paths.extend([md_path, html_path])

        assert md_name is not None and md_name.endswith(".md")
        assert html_name is not None and html_name.endswith(".html")
        assert "# Title <Unsafe>" in Path(md_path).read_text(encoding="utf-8")
        assert "<title>Title &lt;Unsafe&gt;</title>" in Path(html_path).read_text(encoding="utf-8")
    finally:
        for path in written_paths:
            Path(path).unlink(missing_ok=True)


def test_export_formatter_routes_summary_export_from_loaded_data(monkeypatch) -> None:
    formatter = _formatter()

    monkeypatch.setattr(formatter, "_load_summary", lambda summary_id: _summary_data())
    monkeypatch.setattr(formatter, "_export_pdf", lambda data, cid=None: ("p", "f.pdf"))
    monkeypatch.setattr(formatter, "_export_markdown", lambda data, cid=None: ("p", "f.md"))
    monkeypatch.setattr(formatter, "_export_html", lambda data, cid=None: ("p", "f.html"))

    assert formatter.export_summary("1", "pdf") == ("p", "f.pdf")
    assert formatter.export_summary("1", "md") == ("p", "f.md")
    assert formatter.export_summary("1", "html") == ("p", "f.html")
    assert formatter.export_summary("1", "bad") == (None, None)

    monkeypatch.setattr(formatter, "_load_summary", lambda summary_id: None)
    assert formatter.export_summary("missing", "md") == (None, None)
