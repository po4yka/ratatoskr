from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.mcp.article_service import ArticleReadService
from app.mcp.helpers import isotime


class _ScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _ExecuteResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _Session:
    def __init__(
        self,
        *,
        execute_results: list[list[Any]] | None = None,
        scalars_results: list[list[Any]] | None = None,
        scalar_results: list[Any] | None = None,
    ) -> None:
        self._execute_results = list(execute_results or [])
        self._scalars_results = list(scalars_results or [])
        self._scalar_results = list(scalar_results or [])

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, _query: Any) -> _ExecuteResult:
        return _ExecuteResult(self._execute_results.pop(0))

    async def scalars(self, _query: Any) -> _ScalarResult:
        return _ScalarResult(self._scalars_results.pop(0))

    async def scalar(self, _query: Any) -> Any:
        return self._scalar_results.pop(0)


class _Database:
    def __init__(self, session: _Session) -> None:
        self._session = session

    def session(self) -> _Session:
        return self._session


def _context(session: _Session, user_id: int | None = None) -> SimpleNamespace:
    runtime = SimpleNamespace(database=_Database(session))
    return SimpleNamespace(
        user_id=user_id,
        ensure_runtime=lambda: runtime,
        request_scope_filters=lambda _model: [],
    )


def _summary(summary_id: int, request_id: int, title: str, tags: list[str]) -> SimpleNamespace:
    request = SimpleNamespace(
        id=request_id,
        input_url=f"https://example.com/{request_id}",
        normalized_url=f"https://example.com/{request_id}",
        status="completed",
        type="url",
    )
    return SimpleNamespace(
        id=summary_id,
        request=request,
        lang="en",
        is_read=False,
        is_favorited=False,
        created_at=None,
        json_payload={
            "summary_250": f"Summary for {title}",
            "tldr": f"TLDR {title}",
            "topic_tags": tags,
            "metadata": {"title": title, "domain": "example.com"},
        },
    )


def test_isotime_formats_utc_cleanly() -> None:
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 1, 1, 12, 0, 0)

    assert isotime(aware) == "2026-01-01T12:00:00Z"
    assert isotime(naive) == "2026-01-01T12:00:00Z"


@pytest.mark.asyncio
async def test_list_articles_tag_filter_paginates_correctly() -> None:
    old_ai = _summary(1, 101, "Old AI", ["#ai"])
    other = _summary(2, 102, "Other", ["#other"])
    new_ai = _summary(3, 103, "New AI", ["#ai"])
    session = _Session(
        scalars_results=[[new_ai], [old_ai]],
        scalar_results=[2, 2],
    )
    service = ArticleReadService(_context(session, user_id=1))  # type: ignore[arg-type]

    page1 = await service.list_articles(limit=1, offset=0, tag="ai")
    page2 = await service.list_articles(limit=1, offset=1, tag="ai")

    assert page1["total"] == 2
    assert page1["articles"][0]["summary_id"] == 3
    assert page1["has_more"] is True

    assert page2["total"] == 2
    assert page2["articles"][0]["summary_id"] == 1
    assert page2["has_more"] is False


@pytest.mark.asyncio
async def test_search_articles_preserves_fts_order() -> None:
    old_hit = _summary(1, 101, "Old Hit", ["#topic"])
    new_hit = _summary(2, 102, "New Hit", ["#topic"])
    session = _Session(
        execute_results=[[SimpleNamespace(request_id=102), SimpleNamespace(request_id=101)]],
        scalars_results=[[old_hit, new_hit]],
    )

    payload = await ArticleReadService(_context(session, user_id=1)).search_articles(  # type: ignore[arg-type]
        "topic", limit=10
    )

    assert [row["summary_id"] for row in payload["results"]] == [2, 1]  # type: ignore[typeddict-item]
