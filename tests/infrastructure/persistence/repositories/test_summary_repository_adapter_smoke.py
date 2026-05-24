from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.time_utils import UTC
from app.domain.models.request import RequestStatus
from app.domain.models.summary import Summary as DomainSummary
from app.infrastructure.persistence.repositories.summary_repository import (
    SummaryRepositoryAdapter,
    _aggregation_item_to_dict,
    _status_value,
)


class _Result:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []

    def __iter__(self) -> Any:
        return iter(self._rows)

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def one(self) -> Any:
        return self._rows[0]

    def scalars(self) -> _Result:
        return self


class _Session:
    def __init__(self) -> None:
        self.executed: list[Any] = []
        self.feedback = SimpleNamespace(
            id=7,
            rating=4,
            issues='["too_long"]',
            comment="Useful",
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
        )

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def scalar(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def execute(self, stmt: Any, *_args: Any, **_kwargs: Any) -> _Result:
        self.executed.append(stmt)
        return _Result()

    async def scalars(self, *_args: Any, **_kwargs: Any) -> _Result:
        return _Result([self.feedback])

    async def get(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def flush(self) -> None:
        return None


class _Database:
    def __init__(self) -> None:
        self.session_obj = _Session()

    def session(self) -> _Session:
        return self.session_obj

    def transaction(self) -> _Session:
        return self.session_obj


@pytest.mark.asyncio
async def test_summary_repository_empty_database_paths() -> None:
    database = _Database()
    repo = SummaryRepositoryAdapter(database)  # type: ignore[arg-type]

    assert await repo.async_upsert_summary(1, "en", {"tldr": "x"}) == 1
    assert (
        await repo.async_finalize_request_summary(
            1,
            "en",
            {"tldr": "x"},
            request_status=RequestStatus.COMPLETED,
        )
        == 1
    )
    await repo.async_update_summary_insights(1, {"facts": []})
    assert await repo.async_get_user_summaries(1, search="example") == ([], 0, 0)
    assert await repo.async_get_summary_by_request(1) is None
    assert await repo.async_get_summary_id_by_request(1) is None
    assert await repo.async_get_summary_by_id(1) is None
    assert await repo.async_get_summary_context_by_id(1) is None
    assert await repo.async_get_aggregation_source_bundle_for_summary(1) is None
    assert await repo.async_get_summaries_by_request_ids([]) == {}
    assert await repo.async_get_summaries_by_request_ids([1, 2]) == {}
    assert await repo.async_get_unread_summaries(1, 2, limit=0) == []
    assert await repo.async_get_unread_summaries(1, 2, limit=5) == []
    assert await repo.async_get_unread_summary_by_request_id(1) is None
    assert await repo.async_bulk_mark_summaries_as_read(user_id=1, summary_ids=[]) == 0
    assert await repo.async_bulk_mark_summaries_as_read(user_id=1, summary_ids=[1]) == 0
    assert (
        await repo.async_bulk_set_summaries_favorite(
            user_id=1,
            summary_ids=[],
            value=True,
        )
        == 0
    )
    assert (
        await repo.async_bulk_set_summaries_favorite(
            user_id=1,
            summary_ids=[1],
            value=True,
        )
        == 0
    )
    assert await repo.async_bulk_soft_delete_summaries(user_id=1, summary_ids=[]) == 0
    assert await repo.async_bulk_soft_delete_summaries(user_id=1, summary_ids=[1]) == 0
    await repo.async_mark_summary_as_read(1)
    await repo.async_mark_summary_as_unread(1)
    await repo.async_mark_summary_as_read_by_request(1)
    assert await repo.async_get_read_status(1) is False
    await repo.async_update_reading_progress(1, 0.5, 20)
    await repo.async_soft_delete_summary(1)
    await repo.async_set_favorite(1, True)
    assert (
        await repo.async_get_user_summaries_for_insights(
            1,
            datetime(2026, 1, 1, tzinfo=UTC),
            0,
        )
        == []
    )
    assert (
        await repo.async_get_user_summaries_for_insights(
            1,
            datetime(2026, 1, 1, tzinfo=UTC),
            5,
        )
        == []
    )
    assert await repo.async_get_user_summary_activity_dates(
        1,
        datetime(2026, 1, 1, tzinfo=UTC),
    ) == [database.session_obj.feedback]
    assert await repo.async_get_max_server_version(1) is None
    assert await repo.async_get_all_for_user(1) == []
    assert await repo.async_get_summary_for_sync_apply(1, 1) is None
    assert await repo.async_apply_sync_change(1) == 0
    assert await repo.async_apply_sync_change(1, is_read=True, is_deleted=False) == 0

    feedback = await repo.async_upsert_feedback(
        user_id=1,
        summary_id=1,
        rating=4,
        issues=["too_long"],
        comment="Useful",
    )
    assert feedback["issues"] == ["too_long"]

    with pytest.raises(LookupError, match="summary 1 not found"):
        await repo.async_toggle_favorite(1)

    assert database.session_obj.executed


def test_summary_repository_domain_and_search_helpers() -> None:
    repo = SummaryRepositoryAdapter(_Database())  # type: ignore[arg-type]

    domain = repo.to_domain_model(
        {
            "id": 3,
            "request": 9,
            "json_payload": {"tldr": "Short"},
            "lang": "en",
            "version": 2,
            "is_read": True,
            "insights_json": {"facts": []},
            "created_at": "2026-05-01T00:00:00+00:00",
        }
    )
    assert domain.id == 3
    assert domain.request_id == 9
    assert domain.is_read is True

    assert repo.from_domain_model(
        DomainSummary(
            id=4,
            request_id=10,
            content={"tldr": "Short"},
            language="ru",
            version=5,
            is_read=True,
            insights={"facts": []},
        )
    ) == {
        "id": 4,
        "request_id": 10,
        "json_payload": {"tldr": "Short"},
        "lang": "ru",
        "version": 5,
        "is_read": True,
        "insights_json": {"facts": []},
    }

    assert repo._build_tsquery("AI, tools!") == "ai:* & tools:*"
    assert repo._build_tsquery("!!!") is None
    assert repo._sanitize_fts_term("hello, world") == "hello & world"
    assert repo._summary_matches_topic(
        {"topic_tags": ["AI"], "nested": {"title": "Tools"}},
        {"input_url": "https://example.test"},
        "ai tools",
    )
    assert not repo._summary_matches_topic({}, {}, "ai")
    assert _status_value(RequestStatus.COMPLETED) == "ok"
    assert _status_value("custom") == "custom"
    assert _aggregation_item_to_dict(None) is None
