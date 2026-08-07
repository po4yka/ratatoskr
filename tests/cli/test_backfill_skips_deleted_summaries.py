"""The vector-store backfill must not resurrect summaries the user deleted.

Soft-delete leaves ``json_payload`` intact, so a deleted summary still has
content to embed. Without an ``is_deleted`` predicate the backfill re-embedded it
and re-upserted its Qdrant point -- and this CLI is exactly what the
disaster-recovery, Qdrant-setup, backup-restore and embedding-provider-switch
runbooks tell an operator to run, so following any of them silently undid the
user's deletions and made the content searchable again.

The statement the function actually builds is captured and compiled here rather
than grepped for, so a predicate that is present but wired to the wrong column or
dropped by a later refactor still fails.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

from app.cli.backfill_vector_store import _fetch_summaries_page


class _CapturingDb:
    """Stands in for Database, recording the statement handed to execute()."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    def session(self) -> Any:
        statements = self.statements

        @asynccontextmanager
        async def _ctx() -> Any:
            async def _execute(statement: Any) -> Any:
                statements.append(statement)
                return iter(())

            yield SimpleNamespace(execute=_execute)

        return _ctx()


async def _compiled_query() -> str:
    db = _CapturingDb()
    await _fetch_summaries_page(
        cast("Any", db), after_id=0, page_size=10, limit=None, fetched_so_far=0
    )
    assert db.statements, "the page fetch issued no query"
    return str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))


async def test_the_page_query_excludes_deleted_summaries() -> None:
    sql = await _compiled_query()

    assert "is_deleted" in sql, (
        "the backfill selects every summary regardless of is_deleted, so a full "
        "rebuild re-creates Qdrant points for content the user deleted"
    )
    assert "summaries.is_deleted IS false" in sql, (
        f"expected a false-valued is_deleted predicate on summaries; got: {sql}"
    )


async def test_the_page_query_keeps_its_keyset_pagination() -> None:
    """The added predicate must not disturb the id-ordered keyset walk."""
    sql = await _compiled_query()

    assert "summaries.id > 0" in sql
    assert "ORDER BY summaries.id ASC" in sql
    assert "LIMIT 10" in sql


async def test_the_limit_cap_still_short_circuits() -> None:
    """A caller that already fetched its whole limit must issue no query at all."""
    db = _CapturingDb()

    rows = await _fetch_summaries_page(
        cast("Any", db), after_id=0, page_size=10, limit=5, fetched_so_far=5
    )

    assert rows == []
    assert db.statements == []
