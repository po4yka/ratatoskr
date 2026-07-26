from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.dialects import postgresql

from app.infrastructure.persistence.sync_aux_read_adapter import SyncAuxReadAdapter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy import Select


class _EmptyResult:
    @staticmethod
    def mappings() -> list[dict[str, Any]]:
        return []


class _CapturingSession:
    def __init__(self, database: _CapturingDatabase) -> None:
        self._database = database

    async def execute(self, statement: Select[Any]) -> _EmptyResult:
        self._database.statement = statement
        return _EmptyResult()


class _CapturingDatabase:
    def __init__(self) -> None:
        self.statement: Select[Any] | None = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[_CapturingSession]:
        yield _CapturingSession(self)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_type", "excluded_columns"),
    [
        ("request", ("content_text", "error_context_json", "telegram_raw_json")),
        (
            "summary",
            ("insights_json", "ru_payload", "reading_progress", "last_read_offset"),
        ),
        (
            "crawl_result",
            ("content_markdown", "content_html", "raw_response_json", "attempt_log"),
        ),
        (
            "llm_call",
            ("request_messages_json", "response_text", "openrouter_response_json"),
        ),
    ],
)
async def test_sync_page_projects_only_wire_fields_and_applies_keyset_limit(
    entity_type: str,
    excluded_columns: tuple[str, ...],
) -> None:
    database = _CapturingDatabase()
    adapter = SyncAuxReadAdapter(database)  # type: ignore[arg-type]

    rows = await adapter.get_sync_page(
        entity_type,
        42,
        since=5,
        limit=11,
        through_version=20,
    )

    assert rows == []
    assert database.statement is not None
    sql = str(
        database.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "server_version > 5" in sql
    assert "server_version <= 20" in sql
    assert "ORDER BY" in sql
    assert "LIMIT 11" in sql
    for column in excluded_columns:
        assert column not in sql
