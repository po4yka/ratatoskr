from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.mcp.catalog_service import CatalogReadService


class _ScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _RowsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _Session:
    def __init__(
        self,
        *,
        scalar_results: list[Any] | None = None,
        scalars_results: list[list[Any]] | None = None,
        execute_results: list[list[Any]] | None = None,
    ) -> None:
        self._scalar_results = list(scalar_results or [])
        self._scalars_results = list(scalars_results or [])
        self._execute_results = list(execute_results or [])
        self.execute_calls = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def scalar(self, _query: Any) -> Any:
        return self._scalar_results.pop(0)

    async def scalars(self, _query: Any) -> _ScalarResult:
        return _ScalarResult(self._scalars_results.pop(0))

    async def execute(self, _query: Any) -> _RowsResult:
        self.execute_calls += 1
        return _RowsResult(self._execute_results.pop(0))


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
        collection_scope_filters=lambda _model: [],
    )


@pytest.mark.asyncio
async def test_list_videos_uses_runtime_database_session() -> None:
    request = SimpleNamespace(id=9, input_url="https://youtu.be/demo")
    video = SimpleNamespace(
        video_id="demo",
        request=request,
        title="Demo",
        channel="Channel",
        duration_sec=125,
        resolution="1080p",
        view_count=1000,
        like_count=50,
        transcript_text="hello",
        transcript_source="youtube",
        status="completed",
        upload_date="20260101",
        created_at=None,
    )
    session = _Session(scalar_results=[1], scalars_results=[[video]])

    payload = await CatalogReadService(_context(session)).list_videos(status="completed")  # type: ignore[arg-type]

    assert payload["total"] == 1
    assert payload["results"][0] == {
        "video_id": "demo",
        "request_id": 9,
        "url": "https://youtu.be/demo",
        "title": "Demo",
        "channel": "Channel",
        "duration_sec": 125,
        "duration_display": "2:05",
        "resolution": "1080p",
        "view_count": 1000,
        "like_count": 50,
        "has_transcript": True,
        "transcript_source": "youtube",
        "status": "completed",
        "upload_date": "20260101",
        "created_at": "",
    }


@pytest.mark.asyncio
async def test_list_collections_bulk_loads_page_counts() -> None:
    first = SimpleNamespace(
        id=1,
        name="Inbox",
        description=None,
        is_shared=False,
        created_at=None,
        updated_at=None,
    )
    second = SimpleNamespace(
        id=2,
        name="Research",
        description="Saved papers",
        is_shared=True,
        created_at=None,
        updated_at=None,
    )
    session = _Session(
        scalar_results=[2],
        scalars_results=[[first, second]],
        execute_results=[
            [(1, 3)],
            [(1, 2), (2, 1)],
        ],
    )

    payload = await CatalogReadService(_context(session)).list_collections(limit=20, offset=0)  # type: ignore[arg-type]

    assert session.execute_calls == 2
    assert payload["total"] == 2
    assert payload["results"] == [
        {
            "collection_id": 1,
            "name": "Inbox",
            "description": None,
            "item_count": 3,
            "child_collections": 2,
            "is_shared": False,
            "created_at": "",
            "updated_at": "",
        },
        {
            "collection_id": 2,
            "name": "Research",
            "description": "Saved papers",
            "item_count": 0,
            "child_collections": 1,
            "is_shared": True,
            "created_at": "",
            "updated_at": "",
        },
    ]


@pytest.mark.asyncio
async def test_get_video_transcript_returns_text_payload() -> None:
    video = SimpleNamespace(
        video_id="demo",
        title="Demo",
        channel="Channel",
        duration_sec=125,
        transcript_source="youtube",
        subtitle_language="en",
        auto_generated=False,
        transcript_text="hello world",
    )
    session = _Session(scalar_results=[video])

    payload = await CatalogReadService(_context(session)).get_video_transcript("demo")  # type: ignore[arg-type]

    assert payload == {
        "video_id": "demo",
        "title": "Demo",
        "channel": "Channel",
        "duration_sec": 125,
        "transcript_source": "youtube",
        "subtitle_language": "en",
        "auto_generated": False,
        "transcript": "hello world",
        "transcript_length": 11,
        "truncated": False,
    }
