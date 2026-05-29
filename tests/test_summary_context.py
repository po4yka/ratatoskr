"""Tests for summary context builder."""

from __future__ import annotations

import json
import unittest

from app.domain.services.summary_context import build_summary_context


class TestBuildSummaryContext(unittest.TestCase):
    def test_basic_context(self) -> None:
        summary = {
            "json_payload": {
                "title": "Test",
                "summary_1000": "Content",
                "estimated_reading_time_min": 5,
                "source_type": "article",
                "topic_tags": ["ai"],
            },
            "lang": "en",
        }
        request = {"normalized_url": "https://example.com", "input_url": "https://example.com"}
        ctx = build_summary_context(summary, request)
        assert ctx["url"] == "https://example.com"
        assert ctx["title"] == "Test"
        assert ctx["language"] == "en"
        assert ctx["reading_time"] == 5
        assert ctx["source_type"] == "article"
        assert ctx["tags"] == ["ai"]
        assert ctx["content"] == "Content"

    def test_tag_names_override(self) -> None:
        summary = {"json_payload": {"topic_tags": ["old"]}, "lang": "en"}
        request = {"normalized_url": "https://x.com"}
        ctx = build_summary_context(summary, request, tag_names=["new1", "new2"])
        assert ctx["tags"] == ["new1", "new2"]

    def test_none_inputs(self) -> None:
        ctx = build_summary_context(None, None)
        assert ctx["url"] == ""
        assert ctx["title"] == ""
        assert ctx["tags"] == []
        assert ctx["language"] == ""
        assert ctx["reading_time"] == 0
        assert ctx["source_type"] == ""
        assert ctx["content"] == ""

    def test_json_string_payload(self) -> None:
        summary = {"json_payload": json.dumps({"title": "Parsed"}), "lang": "en"}
        ctx = build_summary_context(summary, None)
        assert ctx["title"] == "Parsed"

    def test_invalid_json_string_payload(self) -> None:
        summary = {"json_payload": "not valid json{{{", "lang": "en"}
        ctx = build_summary_context(summary, None)
        assert ctx["title"] == ""

    def test_url_falls_back_to_input_url(self) -> None:
        request = {"input_url": "https://fallback.com"}
        ctx = build_summary_context(None, request)
        assert ctx["url"] == "https://fallback.com"

    def test_normalized_url_preferred_over_input_url(self) -> None:
        request = {
            "normalized_url": "https://normalized.com",
            "input_url": "https://input.com",
        }
        ctx = build_summary_context(None, request)
        assert ctx["url"] == "https://normalized.com"

    def test_content_falls_back_to_summary_250(self) -> None:
        summary = {
            "json_payload": {"summary_250": "Short summary"},
            "lang": "en",
        }
        ctx = build_summary_context(summary, None)
        assert ctx["content"] == "Short summary"

    def test_summary_1000_preferred_over_250(self) -> None:
        summary = {
            "json_payload": {
                "summary_1000": "Long summary",
                "summary_250": "Short summary",
            },
            "lang": "en",
        }
        ctx = build_summary_context(summary, None)
        assert ctx["content"] == "Long summary"

    def test_empty_tag_names_uses_payload_tags(self) -> None:
        summary = {"json_payload": {"topic_tags": ["from_payload"]}, "lang": "en"}
        ctx = build_summary_context(summary, None, tag_names=[])
        # Empty list is falsy, so payload tags should be used
        assert ctx["tags"] == ["from_payload"]


class TestBuildSummaryContextDenormalizedColumns(unittest.TestCase):
    """Verify that denormalized scalar columns and json_payload produce identical context.

    These tests cover audit finding 5C/7A: the metadata fields (title,
    source_type, reading_time, tags) should be sourced from the new columns
    when present, and fall back to json_payload for pre-migration rows.
    """

    _payload = {
        "title": "Column Test",
        "source_type": "article",
        "estimated_reading_time_min": 7,
        "topic_tags": ["ai", "ml"],
        "summary_1000": "Long content",
        "summary_250": "Short content",
    }

    def _summary_from_payload_only(self) -> dict:
        """Simulate a pre-migration row: json_payload only, no scalar columns."""
        return {"json_payload": self._payload, "lang": "en"}

    def _summary_from_columns(self) -> dict:
        """Simulate a post-migration row: scalar columns populated, json_payload also present."""
        return {
            "json_payload": self._payload,
            "lang": "en",
            # denormalized columns
            "title": self._payload["title"],
            "source_type": self._payload["source_type"],
            "reading_time": self._payload["estimated_reading_time_min"],
            "topic_tags": self._payload["topic_tags"],
        }

    def test_columns_and_payload_produce_identical_context(self) -> None:
        """Column-sourced context must equal payload-sourced context exactly."""
        request = {"normalized_url": "https://example.com"}
        ctx_payload = build_summary_context(self._summary_from_payload_only(), request)
        ctx_columns = build_summary_context(self._summary_from_columns(), request)
        assert ctx_columns == ctx_payload, (
            f"Column context differs from payload context:\n"
            f"  columns: {ctx_columns}\n"
            f"  payload: {ctx_payload}"
        )

    def test_columns_preferred_over_payload_for_metadata(self) -> None:
        """When columns differ from payload, columns win for metadata fields."""
        summary = {
            "json_payload": {**self._payload, "title": "Stale Payload Title"},
            "lang": "en",
            "title": "Fresh Column Title",
            "source_type": self._payload["source_type"],
            "reading_time": self._payload["estimated_reading_time_min"],
            "topic_tags": self._payload["topic_tags"],
        }
        ctx = build_summary_context(summary, None)
        assert ctx["title"] == "Fresh Column Title"

    def test_fallback_to_payload_when_columns_none(self) -> None:
        """None column values fall back to json_payload extraction."""
        summary = {
            "json_payload": self._payload,
            "lang": "en",
            "title": None,
            "source_type": None,
            "reading_time": None,
            "topic_tags": None,
        }
        ctx = build_summary_context(summary, None)
        assert ctx["title"] == self._payload["title"]
        assert ctx["source_type"] == self._payload["source_type"]
        assert ctx["reading_time"] == self._payload["estimated_reading_time_min"]
        assert ctx["tags"] == self._payload["topic_tags"]

    def test_content_always_from_payload(self) -> None:
        """content field is always sourced from json_payload, never from columns."""
        summary = {**self._summary_from_columns(), "json_payload": self._payload}
        ctx = build_summary_context(summary, None)
        assert ctx["content"] == self._payload["summary_1000"]

    def test_tag_names_override_column_tags(self) -> None:
        """Explicit tag_names parameter overrides both column and payload tags."""
        summary = self._summary_from_columns()
        ctx = build_summary_context(summary, None, tag_names=["override"])
        assert ctx["tags"] == ["override"]

    def test_empty_tag_names_falls_through_to_column_then_payload(self) -> None:
        """Empty tag_names (falsy) does not suppress column/payload tags."""
        summary = self._summary_from_columns()
        ctx = build_summary_context(summary, None, tag_names=[])
        assert ctx["tags"] == self._payload["topic_tags"]

    def test_column_topic_tags_non_list_falls_back_to_payload(self) -> None:
        """A non-list column value for topic_tags falls back to the payload list."""
        summary = {
            "json_payload": self._payload,
            "lang": "en",
            "topic_tags": "not-a-list",  # invalid type
        }
        ctx = build_summary_context(summary, None)
        assert ctx["tags"] == self._payload["topic_tags"]


class TestExtractSummaryMetadata(unittest.TestCase):
    """Tests for the _extract_summary_metadata write-path helper."""

    def test_extracts_all_fields(self) -> None:
        from app.infrastructure.persistence.repositories.summary_repository import (
            _extract_summary_metadata,
        )

        payload = {
            "title": "My Title",
            "source_type": "article",
            "estimated_reading_time_min": 5,
            "topic_tags": ["a", "b"],
        }
        meta = _extract_summary_metadata(payload)
        assert meta["title"] == "My Title"
        assert meta["source_type"] == "article"
        assert meta["reading_time"] == 5
        assert meta["topic_tags"] == ["a", "b"]

    def test_missing_fields_produce_none(self) -> None:
        from app.infrastructure.persistence.repositories.summary_repository import (
            _extract_summary_metadata,
        )

        meta = _extract_summary_metadata({})
        assert meta["title"] is None
        assert meta["source_type"] is None
        assert meta["reading_time"] is None
        assert meta["topic_tags"] is None

    def test_non_numeric_reading_time_becomes_none(self) -> None:
        from app.infrastructure.persistence.repositories.summary_repository import (
            _extract_summary_metadata,
        )

        meta = _extract_summary_metadata({"estimated_reading_time_min": "NaN"})
        assert meta["reading_time"] is None

    def test_non_list_topic_tags_becomes_none(self) -> None:
        from app.infrastructure.persistence.repositories.summary_repository import (
            _extract_summary_metadata,
        )

        meta = _extract_summary_metadata({"topic_tags": "not-a-list"})
        assert meta["topic_tags"] is None


class TestSyncSnapshotPaging(unittest.TestCase):
    """Unit test proving async_get_all_for_user paginates rather than loading unbounded."""

    def test_pagination_collects_all_pages(self) -> None:
        """Simulate a database with 1300 rows and verify all are returned across pages."""
        import asyncio
        from types import SimpleNamespace
        from app.infrastructure.persistence.repositories.summary_repository import (
            SummaryRepositoryAdapter,
        )

        PAGE_SIZE = SummaryRepositoryAdapter._SYNC_PAGE_SIZE  # 500

        # Build fake row objects with .id attributes
        all_rows = [SimpleNamespace(id=i, request_id=i) for i in range(1, 1301)]

        call_log: list[dict] = []

        class FakeScalars:
            """Iterable wrapper returned by FakeResult.scalars()."""

            def __init__(self, rows: list) -> None:
                self._rows = rows

            def __iter__(self) -> object:
                return iter(self._rows)

        class FakeResult:
            """Wraps a page of rows; mimics sqlalchemy CursorResult."""

            def __init__(self, rows: list) -> None:
                self._rows = rows

            def scalars(self) -> FakeScalars:
                return FakeScalars(self._rows)

        class FakeSession:
            async def __aenter__(self) -> FakeSession:
                return self

            async def __aexit__(self, *_: object) -> None:
                pass

            async def execute(self, stmt: object) -> FakeResult:
                call_log.append({"stmt": stmt})
                page_idx = len(call_log) - 1
                start = page_idx * PAGE_SIZE
                end = start + PAGE_SIZE
                return FakeResult(all_rows[start:end])

        class FakeDatabase:
            def session(self) -> FakeSession:
                return FakeSession()

        repo = SummaryRepositoryAdapter(FakeDatabase())  # type: ignore[arg-type]

        # Monkey-patch model_to_dict to just return a dict with the id
        import app.infrastructure.persistence.repositories.summary_repository as mod

        original_mtd = mod.model_to_dict

        def fake_model_to_dict(row: object | None) -> dict | None:
            if row is None:
                return None
            return {"id": getattr(row, "id", None)}

        mod.model_to_dict = fake_model_to_dict  # type: ignore[assignment]
        try:
            results = asyncio.get_event_loop().run_until_complete(repo.async_get_all_for_user(42))
        finally:
            mod.model_to_dict = original_mtd  # type: ignore[assignment]

        assert len(results) == 1300, f"Expected 1300 rows, got {len(results)}"
        assert len(call_log) == 3, f"Expected 3 pages, got {len(call_log)}"


class TestSmartCollectionNoCap(unittest.TestCase):
    """Unit test proving async_list_user_summaries_with_request has no 10k cap."""

    def test_pagination_collects_beyond_10k(self) -> None:
        """Simulate 11000 rows and verify all are returned (no 10000 hard cap)."""
        import asyncio
        from types import SimpleNamespace
        from app.infrastructure.persistence.repositories.collection_repository import (
            CollectionRepositoryAdapter,
        )

        PAGE_SIZE = CollectionRepositoryAdapter._SMART_SCAN_PAGE_SIZE  # 500

        total = 11000
        # summary objects with .id
        summaries = [SimpleNamespace(id=i, request_id=i) for i in range(1, total + 1)]
        # request objects (no .id needed for model_to_dict mock)
        requests = [SimpleNamespace(id=i) for i in range(1, total + 1)]
        all_rows = list(zip(summaries, requests, strict=True))

        call_log: list[int] = []

        class FakeResult:
            def __init__(self, rows: list) -> None:
                self._rows = rows

            def all(self) -> list:
                return self._rows

        class FakeSession:
            async def __aenter__(self) -> FakeSession:
                return self

            async def __aexit__(self, *_: object) -> None:
                pass

            async def execute(self, stmt: object) -> FakeResult:
                page_idx = len(call_log)
                call_log.append(page_idx)
                start = page_idx * PAGE_SIZE
                end = start + PAGE_SIZE
                return FakeResult(all_rows[start:end])

        class FakeDatabase:
            def session(self) -> FakeSession:
                return FakeSession()

        repo = CollectionRepositoryAdapter(FakeDatabase())  # type: ignore[arg-type]

        import app.infrastructure.persistence.repositories.collection_repository as col_mod

        original_mtd = col_mod.model_to_dict

        def fake_model_to_dict(row: object | None) -> dict | None:
            if row is None:
                return None
            return {"id": getattr(row, "id", None)}

        col_mod.model_to_dict = fake_model_to_dict  # type: ignore[assignment]
        try:
            results = asyncio.get_event_loop().run_until_complete(
                repo.async_list_user_summaries_with_request(99)
            )
        finally:
            col_mod.model_to_dict = original_mtd  # type: ignore[assignment]

        assert len(results) == total, f"Expected {total} rows, got {len(results)}"
        expected_pages = total // PAGE_SIZE  # 22 full pages + 0 remainder = 22 pages + 1 empty
        assert len(call_log) == expected_pages + 1, (
            f"Expected {expected_pages + 1} DB calls (last returns empty), got {len(call_log)}"
        )


if __name__ == "__main__":
    unittest.main()
