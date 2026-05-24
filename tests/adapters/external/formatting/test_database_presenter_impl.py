from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.external.formatting.database_presenter import DatabasePresenterImpl
from app.application.dto.topic_search import TopicArticle


class _Formatter:
    def format_bytes(self, size: int) -> str:
        return f"{size} B"


def _presenter() -> tuple[DatabasePresenterImpl, SimpleNamespace]:
    sender = SimpleNamespace(safe_reply=AsyncMock())
    return DatabasePresenterImpl(sender, _Formatter()), sender


def _sent_text(sender: SimpleNamespace) -> str:
    return sender.safe_reply.await_args.args[1]


@pytest.mark.asyncio
async def test_database_presenter_sends_database_overview() -> None:
    presenter, sender = _presenter()

    await presenter.send_db_overview(
        "message",
        {
            "path_display": "/tmp/app.db",
            "db_size_bytes": 4096,
            "tables": {"summaries": 2, "requests": 3},
            "tables_truncated": 4,
            "total_requests": 10,
            "total_summaries": 7,
            "requests_by_status": {"": 1, "done": 9},
            "last_request_at": "2026-05-01",
            "last_summary_at": "2026-05-02",
            "last_audit_at": "2026-05-03",
            "errors": ["first", "second"],
        },
    )

    text = _sent_text(sender)
    assert "Database Overview" in text
    assert "Path: `/tmp/app.db`" in text
    assert "Size: 4096 B (4,096 bytes)" in text
    assert "- requests: 3" in text
    assert "- summaries: 2" in text
    assert "- ...and 4 more (not displayed)" in text
    assert "Totals: Requests: 10, Summaries: 7" in text
    assert "- unknown: 1" in text
    assert "- done: 9" in text
    assert "Last audit log: 2026-05-03" in text
    assert "Warnings:" in text


@pytest.mark.asyncio
async def test_database_presenter_sends_topic_search_results_for_known_sources() -> None:
    presenter, sender = _presenter()
    long_topic = " ".join(["topic"] * 40)
    long_title = "A" * 190

    await presenter.send_topic_search_results(
        "message",
        topic=long_topic,
        source="library",
        articles=[
            TopicArticle(
                title=long_title,
                url="https://example.test/a",
                snippet="short snippet",
                source="Example",
                published_at="2026-05-01",
            )
        ],
    )

    library_text = _sent_text(sender)
    assert "Saved library results for:" in library_text
    assert "..." in library_text
    assert "1. " + ("A" * 177) + "..." in library_text
    assert "https://example.test/a" in library_text
    assert "Example" in library_text
    assert "short snippet" in library_text
    assert "/read <request_id>" in library_text

    await presenter.send_topic_search_results(
        "message",
        topic="  ",
        source="online",
        articles=[TopicArticle(title="", url="https://example.test/b")],
    )

    online_text = _sent_text(sender)
    assert "Online search results for: your topic" in online_text
    assert "1. https://example.test/b" in online_text
    assert "detailed summary" in online_text

    await presenter.send_topic_search_results(
        "message",
        topic="custom",
        source="unknown",
        articles=[],
    )

    assert "Search results for: custom" in _sent_text(sender)


@pytest.mark.asyncio
async def test_database_presenter_sends_db_verification_with_truncated_sections() -> None:
    presenter, sender = _presenter()
    missing_summary = [
        {"request_id": idx, "type": "url", "status": "done", "source": f"source-{idx}"}
        for idx in range(6)
    ]
    missing_fields = [
        {
            "request_id": idx,
            "type": "url",
            "status": "done",
            "source": f"source-{idx}",
            "missing": ["a", "b", "c", "d", "e", "f", "g"],
        }
        for idx in range(6)
    ]
    missing_links = [
        {"request_id": idx, "reason": "empty", "source": f"source-{idx}"} for idx in range(6)
    ]
    reprocess = [
        {
            "request_id": idx,
            "normalized_url": f"https://example.test/{idx}",
            "reasons": ["summary", "fields", "links", "audit", "extra"],
        }
        for idx in range(6)
    ]

    await presenter.send_db_verification(
        "message",
        {
            "overview": {
                "path_display": "/tmp/app.db",
                "db_size_bytes": 2048,
                "tables": {"requests": 1},
                "requests_by_status": {"": 1},
            },
            "posts": {
                "required_fields": [f"field_{idx}" for idx in range(9)],
                "checked": 20,
                "with_summary": 12,
                "missing_summary": missing_summary,
                "missing_fields": missing_fields,
                "links": {
                    "total_links": 15,
                    "posts_with_links": 8,
                    "missing_data": missing_links,
                },
                "errors": [f"warning-{idx}" for idx in range(6)],
                "reprocess": reprocess,
            },
        },
    )

    text = _sent_text(sender)
    assert "Database Verification" in text
    assert "Path: `/tmp/app.db`" in text
    assert "Size: 2048 B (2,048 bytes)" in text
    assert "Fields checked: field_0" in text
    assert "Posts checked: 20" in text
    assert "With summary: 12" in text
    assert "Missing summaries: 6" in text
    assert "Missing fields detected: 6" in text
    assert "Link coverage:" in text
    assert "- Total captured links: 15" in text
    assert "- Missing link data: 6" in text
    assert "warning-0" in text
    assert "Reprocess queue: 6 posts" in text
    assert "Please reprocess the affected posts" in text


@pytest.mark.asyncio
async def test_database_presenter_reports_reprocess_start_branches() -> None:
    presenter, sender = _presenter()
    targets = [
        {
            "request_id": idx,
            "url": f"https://example.test/{idx}",
            "reasons": ["summary", "fields", "links", "audit", "extra"],
        }
        for idx in range(6)
    ]

    await presenter.send_db_reprocess_start(
        "message",
        url_targets=targets,
        skipped=[{"request_id": 99}],
    )

    text = _sent_text(sender)
    assert "Starting automated reprocessing" in text
    assert "Processing 6 URL posts..." in text
    assert "https://example.test/0" in text
    assert "1 more URLs" in text
    assert "Skipped 1 posts that require manual attention" in text

    await presenter.send_db_reprocess_start("message", url_targets=[], skipped=[])

    assert "No URL posts available for automatic reprocessing." in _sent_text(sender)


@pytest.mark.asyncio
async def test_database_presenter_reports_reprocess_completion_branches() -> None:
    presenter, sender = _presenter()
    targets = [{"request_id": idx, "url": f"https://example.test/{idx}"} for idx in range(8)]
    failures = [
        {"request_id": idx, "url": f"https://example.test/{idx}", "error": ""} for idx in range(6)
    ]

    await presenter.send_db_reprocess_complete(
        "message",
        url_targets=targets,
        failures=failures,
        skipped=[{"request_id": 99}],
    )

    text = _sent_text(sender)
    assert "Reprocessing complete" in text
    assert "Processed 2/8 URL posts." in text
    assert "Failures:" in text
    assert "unknown error" in text
    assert "1 more failures" in text
    assert "Skipped 1 posts that could not be retried automatically." in text

    await presenter.send_db_reprocess_complete(
        "message",
        url_targets=targets,
        failures=[],
        skipped=[],
    )

    assert "Processed 8/8 URL posts." in _sent_text(sender)
