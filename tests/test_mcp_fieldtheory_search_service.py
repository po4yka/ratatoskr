"""Behavioural tests for ``FieldTheorySearchService``.

Most tests are Postgres-gated (mirroring ``tests/adapters/ingestors/test_fieldtheory_ingestor.py``): the ``session`` and ``database`` fixtures skip when ``TEST_DATABASE_URL`` is unset, so the Postgres FTS path is exercised in the integration job rather than every laptop ``pytest`` invocation. The invalid-category test runs without Postgres because the service short-circuits before any SQL is built.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from app.core.url_utils import compute_dedupe_hash, normalize_url
from app.db.models.core import FieldTheoryBookmarkMetadata
from app.domain.models.request import RequestStatus
from app.domain.models.source import SourceKind
from app.mcp.fieldtheory_search_service import FieldTheorySearchService
from tests.db_helpers_async import create_request

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.session import Database


pytestmark = pytest.mark.asyncio


def _context(database: Database) -> SimpleNamespace:
    """Build a stub ``McpServerContext`` backed by the real test database."""
    runtime = SimpleNamespace(database=database)
    return SimpleNamespace(
        user_id=None,
        ensure_runtime=lambda: runtime,
        request_scope_filters=lambda _model: [],
    )


async def _seed_bookmark(
    session: AsyncSession,
    *,
    fieldtheory_id: str,
    url: str,
    tweet_text: str,
    category: str = "tool",
    author: str = "tester",
    posted_at: dt.datetime | None = None,
    correlation_id: str | None = None,
) -> int:
    """Insert a Request + FieldTheoryBookmarkMetadata pair and return request_id."""
    request_id = await create_request(
        session,
        type_=SourceKind.FIELDTHEORY_BOOKMARK.value,
        status=RequestStatus.FIELDTHEORY_IMPORTED.value,
        correlation_id=correlation_id or f"corr-{fieldtheory_id}",
        input_url=url,
        normalized_url=normalize_url(url),
        dedupe_hash=compute_dedupe_hash(url),
    )
    session.add(
        FieldTheoryBookmarkMetadata(
            request_id=request_id,
            fieldtheory_id=fieldtheory_id,
            fieldtheory_category=category,
            tweet_text=tweet_text,
            tweet_author=author,
            tweet_url=url,
            posted_at=posted_at,
            synced_at=dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC),
        )
    )
    await session.commit()
    return request_id


async def test_empty_corpus_returns_empty_results(
    session: AsyncSession,
    database: Database,
) -> None:
    """No bookmarks in the corpus → search returns an empty results list."""
    service = FieldTheorySearchService(context=cast("Any", _context(database)))

    result = await service.search("anything")

    assert result == {"results": [], "query": "anything", "category": None}


async def test_single_match_returns_single_row(
    session: AsyncSession,
    database: Database,
) -> None:
    """One matching bookmark → one result whose shape matches the design doc.

    Specifically asserts the API-facing field name is ``canonical_url`` (projected from ``Request.normalized_url`` by the service per DEC-005), not ``normalized_url``.
    """
    url = "https://twitter.com/alice/status/1"
    request_id = await _seed_bookmark(
        session,
        fieldtheory_id="ft-kelly",
        url=url,
        tweet_text="kelly criterion betting strategy explained",
        category="tool",
        author="alice",
        posted_at=dt.datetime(2026, 1, 5, 12, 0, tzinfo=dt.UTC),
    )

    service = FieldTheorySearchService(context=cast("Any", _context(database)))
    result = await service.search("kelly")

    assert "error" not in result
    assert result["query"] == "kelly"
    assert result["category"] is None
    rows = result["results"]
    assert len(rows) == 1
    row = rows[0]
    assert row["request_id"] == request_id
    # API-facing field is canonical_url, projected from normalized_url
    assert row["canonical_url"] == normalize_url(url)
    assert "normalized_url" not in row
    assert row["category"] == "tool"
    assert row["tweet_text"] == "kelly criterion betting strategy explained"
    assert row["tweet_author"] == "alice"
    assert row["posted_at"] is not None
    assert isinstance(row["rank"], float)
    assert row["rank"] > 0


async def test_category_filter_narrows_results(
    session: AsyncSession,
    database: Database,
) -> None:
    """``category`` arg constrains results to that category only.

    Same matching text in two rows under different categories; no-filter returns both, ``category='tool'`` returns only the tool row.
    """
    body = "foo bar baz unique phrase"
    rid_tool = await _seed_bookmark(
        session,
        fieldtheory_id="ft-cat-tool",
        url="https://twitter.com/a/status/1",
        tweet_text=body,
        category="tool",
        author="alice",
        posted_at=dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC),
    )
    rid_research = await _seed_bookmark(
        session,
        fieldtheory_id="ft-cat-research",
        url="https://twitter.com/b/status/2",
        tweet_text=body,
        category="research",
        author="bob",
        posted_at=dt.datetime(2026, 1, 2, 12, 0, tzinfo=dt.UTC),
    )

    service = FieldTheorySearchService(context=cast("Any", _context(database)))

    unfiltered = await service.search("foo bar baz")
    assert "error" not in unfiltered
    assert {r["request_id"] for r in unfiltered["results"]} == {rid_tool, rid_research}

    filtered = await service.search("foo bar baz", category="tool")
    assert "error" not in filtered
    assert filtered["category"] == "tool"
    assert len(filtered["results"]) == 1
    assert filtered["results"][0]["request_id"] == rid_tool
    assert filtered["results"][0]["category"] == "tool"


async def test_rank_ordering_by_ts_rank_cd_then_posted_at(
    session: AsyncSession,
    database: Database,
) -> None:
    """Results sort by ``ts_rank_cd`` desc, then ``posted_at`` desc NULLS LAST.

    Three bookmarks all matching the AND-semantic ``plainto_tsquery``:

    * Row A: query terms appear adjacent (positions 1-3), short doc, newer ``posted_at``
    * Row B: identical text to A so ``ts_rank_cd`` ties with A; older ``posted_at`` so the tiebreak places it below A
    * Row C: same query terms but spread across a much longer doc → larger cover span → strictly lower ``ts_rank_cd``; newest ``posted_at`` is dominated by the rank order so it lands last

    The SUT uses ``plainto_tsquery`` which builds an AND query, so all three rows must contain every query lexeme to match at all; rank order then separates them.
    """
    rid_a = await _seed_bookmark(
        session,
        fieldtheory_id="ft-rank-A",
        url="https://twitter.com/x/status/1",
        tweet_text="rust async runtime tokio deep dive",
        category="tool",
        author="alice",
        posted_at=dt.datetime(2026, 1, 10, 12, 0, tzinfo=dt.UTC),
    )
    rid_b = await _seed_bookmark(
        session,
        fieldtheory_id="ft-rank-B",
        url="https://twitter.com/y/status/2",
        tweet_text="rust async runtime tokio deep dive",
        category="tool",
        author="bob",
        posted_at=dt.datetime(2026, 1, 5, 12, 0, tzinfo=dt.UTC),
    )
    rid_c = await _seed_bookmark(
        session,
        fieldtheory_id="ft-rank-C",
        url="https://twitter.com/z/status/3",
        # All 3 query lexemes present but spread out across a long sentence — the cover span (first-to-last matched lexeme) is much larger than in A/B, so ts_rank_cd is strictly lower.
        tweet_text=(
            "rust is one of many systems languages we discussed today and the "
            "ergonomics around async programming are great but the runtime "
            "characteristics can be surprising for newcomers to the ecosystem"
        ),
        category="tool",
        author="carol",
        posted_at=dt.datetime(2026, 1, 20, 12, 0, tzinfo=dt.UTC),
    )

    service = FieldTheorySearchService(context=cast("Any", _context(database)))
    result = await service.search("rust async runtime")

    assert "error" not in result
    ordered_ids = [row["request_id"] for row in result["results"]]
    assert ordered_ids == [rid_a, rid_b, rid_c]
    # Sanity: row A's rank must be >= row B's, and both must exceed row C's
    ranks = [row["rank"] for row in result["results"]]
    assert ranks[0] >= ranks[1] > ranks[2]


async def test_invalid_category_returns_error_envelope_without_db_call() -> None:
    """Invalid category surfaces an MCP error envelope before any SQL is built.

    The handler validates ``category`` against ``_VALID_CATEGORY_VALUES`` BEFORE calling ``runtime.database.session()``. Pass a context whose database would raise on ``.session()``, then assert we get the error envelope back — proving the validation short-circuits. This test does not require Postgres.
    """

    class _ExplodingDatabase:
        def session(self) -> Any:
            msg = "database.session() must not be called for invalid category"
            raise AssertionError(msg)

    runtime = SimpleNamespace(database=_ExplodingDatabase())
    context = SimpleNamespace(
        user_id=None,
        ensure_runtime=lambda: runtime,
        request_scope_filters=lambda _model: [],
    )
    service = FieldTheorySearchService(context=cast("Any", context))

    result = await service.search("anything", category="not-a-real-category")

    assert "error" in result
    assert "Invalid category" in result["error"]
    assert "not-a-real-category" in result["error"]
