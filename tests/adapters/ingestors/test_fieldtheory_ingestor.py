"""Behavioural tests for ``FieldTheoryBookmarkIngestor``.

Postgres-gated: the ``session`` fixture skips when ``TEST_DATABASE_URL`` is
unset, so these tests run in the integration job rather than every laptop
``pytest`` invocation.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.adapters.ingestors.fieldtheory_ingestor import (
    FieldTheoryBookmarkIngestor,
    FieldTheoryBookmarkRow,
)
from app.core.url_utils import compute_dedupe_hash, normalize_url
from app.db.models.core import FieldTheoryBookmarkMetadata, FieldTheoryCategory, Request
from app.domain.models.request import RequestStatus
from app.domain.models.source import SourceKind
from tests.db_helpers_async import create_request

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.session import Database


pytestmark = pytest.mark.asyncio


def _row(
    *,
    fieldtheory_id: str,
    url: str,
    primary_category: str | None = "tool",
    tweet_text: str | None = "hello world",
    tweet_author: str | None = "alice",
    posted_at: dt.datetime | None = None,
    synced_at: dt.datetime | None = None,
) -> FieldTheoryBookmarkRow:
    return FieldTheoryBookmarkRow(
        fieldtheory_id=fieldtheory_id,
        url=url,
        tweet_text=tweet_text,
        tweet_author=tweet_author,
        primary_category=primary_category,
        posted_at=posted_at,
        synced_at=synced_at or dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC),
    )


class _StubIngestor(FieldTheoryBookmarkIngestor):
    """Test variant that injects deterministic rows instead of reading a file."""

    def __init__(
        self,
        *,
        database: Database,
        rows: list[FieldTheoryBookmarkRow],
    ) -> None:
        super().__init__(database=database, bookmarks_db_path=pathlib.Path("/nonexistent"))
        self._rows = rows

    async def _iter_bookmarks(self, *, after: dt.datetime | None):
        for row in self._rows:
            if after is None or row.synced_at > after:
                yield row


async def test_miss_path_inserts_request_and_metadata(
    session: AsyncSession,
    database: Database,
) -> None:
    ingestor = _StubIngestor(
        database=database,
        rows=[
            _row(
                fieldtheory_id="ft-1",
                url="https://twitter.com/alice/status/1",
                primary_category="tool",
            ),
        ],
    )

    stats = await ingestor.sync()

    assert stats.bookmarks_seen == 1
    assert stats.requests_created == 1
    assert stats.metadata_inserted == 1
    assert stats.metadata_updated == 0
    assert stats.skipped_invalid_category == 0

    async with database.session() as verify:
        request = (
            await verify.execute(
                select(Request).where(Request.input_url == "https://twitter.com/alice/status/1")
            )
        ).scalar_one()
        assert request.type == SourceKind.FIELDTHEORY_BOOKMARK.value
        assert request.status == RequestStatus.FIELDTHEORY_IMPORTED.value
        assert request.correlation_id and len(request.correlation_id) == 32
        assert request.dedupe_hash == compute_dedupe_hash("https://twitter.com/alice/status/1")
        assert request.normalized_url == normalize_url("https://twitter.com/alice/status/1")

        metadata = (
            await verify.execute(
                select(FieldTheoryBookmarkMetadata).where(
                    FieldTheoryBookmarkMetadata.fieldtheory_id == "ft-1"
                )
            )
        ).scalar_one()
        assert metadata.request_id == request.id
        assert metadata.fieldtheory_category == "tool"
        assert metadata.tweet_text == "hello world"
        assert metadata.tweet_url == "https://twitter.com/alice/status/1"
        assert metadata.tweet_author == "alice"


async def test_hit_path_upserts_metadata_and_preserves_request(
    session: AsyncSession,
    database: Database,
) -> None:
    url = "https://twitter.com/bob/status/42"
    dedupe_hash = compute_dedupe_hash(url)
    existing_id = await create_request(
        session,
        type_="url_processing",
        status="succeeded",
        correlation_id="preexisting-correlation-id",
        input_url=url,
        normalized_url=normalize_url(url),
        dedupe_hash=dedupe_hash,
    )
    await session.commit()

    ingestor = _StubIngestor(
        database=database,
        rows=[
            _row(
                fieldtheory_id="ft-42",
                url=url,
                primary_category="security",
                tweet_text="bob's tweet",
                tweet_author="bob",
            ),
        ],
    )

    stats = await ingestor.sync()

    assert stats.requests_created == 0
    assert stats.metadata_inserted == 1
    assert stats.metadata_updated == 0

    async with database.session() as verify:
        request = await verify.get(Request, existing_id)
        assert request is not None
        # Request must be left exactly as it was.
        assert request.type == "url_processing"
        assert request.status == "succeeded"
        assert request.correlation_id == "preexisting-correlation-id"

        metadata = (
            await verify.execute(
                select(FieldTheoryBookmarkMetadata).where(
                    FieldTheoryBookmarkMetadata.fieldtheory_id == "ft-42"
                )
            )
        ).scalar_one()
        assert metadata.request_id == existing_id
        assert metadata.fieldtheory_category == "security"
        assert metadata.tweet_text == "bob's tweet"


async def test_re_run_is_a_no_op_and_preserves_correlation_id(
    session: AsyncSession,
    database: Database,
) -> None:
    row = _row(
        fieldtheory_id="ft-noop",
        url="https://twitter.com/carol/status/7",
        primary_category="technique",
        tweet_text="initial",
    )
    ingestor = _StubIngestor(database=database, rows=[row])

    first = await ingestor.sync()
    assert first.requests_created == 1
    assert first.metadata_inserted == 1
    assert first.metadata_updated == 0

    async with database.session() as verify:
        request_after_first = (
            await verify.execute(
                select(Request).where(Request.input_url == "https://twitter.com/carol/status/7")
            )
        ).scalar_one()
        first_correlation_id = request_after_first.correlation_id

    second = await ingestor.sync()
    # Identical re-run: the watermark advanced past row.synced_at on the first
    # pass (``WHERE synced_at > :after``), so the stub yields nothing — a true
    # no-op. Path C is exercised separately by
    # ``test_path_c_updates_metadata_when_fieldtheory_id_resyncs``.
    assert second.bookmarks_seen == 0
    assert second.requests_created == 0
    assert second.metadata_inserted == 0
    assert second.metadata_updated == 0

    third = await ingestor.sync()
    assert third.bookmarks_seen == 0
    assert third.requests_created == 0
    assert third.metadata_inserted == 0
    assert third.metadata_updated == 0

    async with database.session() as verify:
        # Still exactly one Request, with the correlation_id we captured.
        rows = (
            (
                await verify.execute(
                    select(Request).where(Request.input_url == "https://twitter.com/carol/status/7")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].correlation_id == first_correlation_id


async def test_path_c_updates_metadata_when_fieldtheory_id_resyncs(
    session: AsyncSession,
    database: Database,
) -> None:
    """When ft re-syncs the same bookmark with a fresher ``synced_at`` (e.g. the
    category was re-classified or the tweet text refreshed), Path C must
    update the existing metadata row in place and leave the Request alone.
    """
    initial_synced_at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    refreshed_synced_at = dt.datetime(2026, 1, 2, 12, 0, tzinfo=dt.UTC)

    first_row = _row(
        fieldtheory_id="ft-resync",
        url="https://twitter.com/diana/status/8",
        primary_category="tool",
        tweet_text="initial body",
        synced_at=initial_synced_at,
    )
    ingestor = _StubIngestor(database=database, rows=[first_row])
    first = await ingestor.sync()
    assert first.requests_created == 1
    assert first.metadata_inserted == 1
    assert first.metadata_updated == 0

    refreshed_row = _row(
        fieldtheory_id="ft-resync",
        url="https://twitter.com/diana/status/8",
        primary_category="research",
        tweet_text="refreshed body",
        synced_at=refreshed_synced_at,
    )
    ingestor_refreshed = _StubIngestor(database=database, rows=[refreshed_row])
    second = await ingestor_refreshed.sync()
    assert second.bookmarks_seen == 1
    assert second.requests_created == 0
    assert second.metadata_inserted == 0
    assert second.metadata_updated == 1

    async with database.session() as verify:
        metadata = (
            await verify.execute(
                select(FieldTheoryBookmarkMetadata).where(
                    FieldTheoryBookmarkMetadata.fieldtheory_id == "ft-resync"
                )
            )
        ).scalar_one()
        assert metadata.fieldtheory_category == "research"
        assert metadata.tweet_text == "refreshed body"
        assert metadata.synced_at == refreshed_synced_at


async def test_invalid_category_is_skipped_not_raised(
    session: AsyncSession,
    database: Database,
) -> None:
    ingestor = _StubIngestor(
        database=database,
        rows=[
            _row(
                fieldtheory_id="ft-bogus",
                url="https://twitter.com/dave/status/9",
                primary_category="bogus",
            ),
            _row(
                fieldtheory_id="ft-unclassified",
                url="https://twitter.com/eve/status/10",
                primary_category="unclassified",
            ),
            _row(
                fieldtheory_id="ft-valid",
                url="https://twitter.com/frank/status/11",
                primary_category="research",
            ),
        ],
    )

    stats = await ingestor.sync()

    assert stats.bookmarks_seen == 3
    assert stats.skipped_invalid_category == 2
    assert stats.requests_created == 1
    assert stats.metadata_inserted == 1

    async with database.session() as verify:
        count = (await verify.execute(select(FieldTheoryBookmarkMetadata))).scalars().all()
        assert {meta.fieldtheory_id for meta in count} == {"ft-valid"}


_BOOKMARKS_FIXTURE_DDL = """
CREATE TABLE bookmarks (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    text TEXT,
    author_handle TEXT,
    primary_category TEXT,
    posted_at TEXT,
    synced_at TEXT NOT NULL
)
"""


def _seed_bookmarks_sqlite(
    path: pathlib.Path,
    rows: list[tuple[str, str, str | None, str | None, str | None, str | None, str]],
) -> None:
    """Build a minimal ``bookmarks.db`` fixture mirroring ft's schema subset."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_BOOKMARKS_FIXTURE_DDL)
        conn.executemany(
            "INSERT INTO bookmarks (id, url, text, author_handle, primary_category, "
            "posted_at, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


async def test_real_bookmarks_db_fixture_round_trip_is_idempotent(
    session: AsyncSession,
    database: Database,
    tmp_path: pathlib.Path,
) -> None:
    """Exercises ``_iter_bookmarks`` against a real aiosqlite read; two passes
    over the same file produce the same end state (Path C on the second pass).
    """
    fixture = tmp_path / "bookmarks.db"
    _seed_bookmarks_sqlite(
        fixture,
        rows=[
            (
                "ft-real-1",
                "https://twitter.com/gina/status/100",
                "first",
                "gina",
                "tool",
                "2025-12-30T09:00:00+00:00",
                "2026-01-01T10:00:00+00:00",
            ),
            (
                "ft-real-2",
                "https://twitter.com/hank/status/101",
                "second",
                "hank",
                "research",
                "2025-12-30T09:30:00+00:00",
                "2026-01-01T11:00:00+00:00",
            ),
        ],
    )

    ingestor = FieldTheoryBookmarkIngestor(database=database, bookmarks_db_path=fixture)

    first = await ingestor.sync()
    assert first.bookmarks_seen == 2
    assert first.requests_created == 2
    assert first.metadata_inserted == 2
    assert first.metadata_updated == 0
    assert first.skipped_invalid_category == 0
    assert first.skipped_invalid_url == 0

    second = await ingestor.sync()
    # Watermark advances past the highest synced_at on the first pass; the
    # delta-scan query returns no rows, so the second pass is a true no-op.
    assert second.bookmarks_seen == 0
    assert second.requests_created == 0
    assert second.metadata_inserted == 0
    assert second.metadata_updated == 0

    async with database.session() as verify:
        request_count = await verify.scalar(
            select(func.count())
            .select_from(Request)
            .where(Request.type == SourceKind.FIELDTHEORY_BOOKMARK.value)
        )
        metadata_count = await verify.scalar(
            select(func.count()).select_from(FieldTheoryBookmarkMetadata)
        )
    assert request_count == 2
    assert metadata_count == 2


async def test_db_check_constraint_rejects_unknown_category(
    session: AsyncSession,
    database: Database,
) -> None:
    """The CHECK constraint at the DB layer is the last line of defence; the
    ingestor pre-filters bogus categories, but a malformed direct INSERT must
    still be rejected.
    """
    request_id = await create_request(
        session,
        type_=SourceKind.FIELDTHEORY_BOOKMARK.value,
        status=RequestStatus.FIELDTHEORY_IMPORTED.value,
        correlation_id="ck-test-correlation-id-00000000",
        input_url="https://twitter.com/zoe/status/999",
        normalized_url=normalize_url("https://twitter.com/zoe/status/999"),
        dedupe_hash=compute_dedupe_hash("https://twitter.com/zoe/status/999"),
    )
    await session.commit()

    async with database.session() as insert_session:
        insert_session.add(
            FieldTheoryBookmarkMetadata(
                request_id=request_id,
                fieldtheory_id="ft-ck-bogus",
                fieldtheory_category="bogus_not_in_enum",
                tweet_text="will not land",
                tweet_author="zoe",
                tweet_url="https://twitter.com/zoe/status/999",
                posted_at=None,
                synced_at=dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC),
            )
        )
        with pytest.raises(IntegrityError):
            await insert_session.commit()


async def test_url_normalization_drives_dedupe_across_tracking_params(
    session: AsyncSession,
    database: Database,
) -> None:
    """Two FT bookmarks whose URLs differ only in tracking params dedupe to a
    single ``requests`` row. Because ``fieldtheory_bookmark_metadata.request_id``
    is the PK (1:1 with Request — see design doc table at line 62), the second
    FT bookmark cannot claim the same metadata slot: the ingestor logs the
    collision and increments ``skipped_metadata_slot_taken``. The first
    bookmark wins; the second is dropped from our 1:1 mirror (ft itself
    keeps both rows on its side).
    """
    url_clean = "https://example.com/article"
    url_tracked = "https://example.com/article?utm_source=newsletter&utm_campaign=feb26"
    # Sanity: the two URLs collapse to the same dedupe_hash.
    assert compute_dedupe_hash(url_clean) == compute_dedupe_hash(url_tracked)

    ingestor = _StubIngestor(
        database=database,
        rows=[
            _row(
                fieldtheory_id="ft-dedupe-1",
                url=url_clean,
                primary_category="tool",
                tweet_text="clean variant",
                synced_at=dt.datetime(2026, 1, 1, 9, 0, tzinfo=dt.UTC),
            ),
            _row(
                fieldtheory_id="ft-dedupe-2",
                url=url_tracked,
                primary_category="tool",
                tweet_text="tracked variant",
                synced_at=dt.datetime(2026, 1, 1, 9, 30, tzinfo=dt.UTC),
            ),
        ],
    )

    stats = await ingestor.sync()

    assert stats.bookmarks_seen == 2
    assert stats.requests_created == 1  # Path A for row 1 only.
    assert stats.metadata_inserted == 1  # Row 2 collides — Path B detects and skips.
    assert stats.metadata_updated == 0
    assert stats.skipped_metadata_slot_taken == 1

    async with database.session() as verify:
        requests = (
            (
                await verify.execute(
                    select(Request).where(Request.normalized_url == normalize_url(url_clean))
                )
            )
            .scalars()
            .all()
        )
        assert len(requests) == 1
        shared_request_id = requests[0].id

        metadata_rows = (await verify.execute(select(FieldTheoryBookmarkMetadata))).scalars().all()
        # First-wins: only ft-dedupe-1 is mirrored; ft-dedupe-2 collides and is
        # logged-and-skipped rather than overwriting the first claim.
        assert {m.fieldtheory_id for m in metadata_rows} == {"ft-dedupe-1"}
        assert metadata_rows[0].request_id == shared_request_id
        assert metadata_rows[0].tweet_text == "clean variant"


async def test_all_seven_fieldtheory_categories_round_trip(
    session: AsyncSession,
    database: Database,
) -> None:
    """The closed v2 category vocabulary has exactly seven members; the
    ingestor must accept every one and persist its string value verbatim.
    """
    categories = list(FieldTheoryCategory)
    assert len(categories) == 7, "Category enum drift — update test + migration in lockstep"

    base_synced_at = dt.datetime(2026, 1, 1, 8, 0, tzinfo=dt.UTC)
    rows = [
        _row(
            fieldtheory_id=f"ft-cat-{idx}",
            url=f"https://twitter.com/user{idx}/status/{1000 + idx}",
            primary_category=category.value,
            tweet_text=f"sample for {category.value}",
            tweet_author=f"user{idx}",
            synced_at=base_synced_at + dt.timedelta(minutes=idx),
        )
        for idx, category in enumerate(categories)
    ]

    ingestor = _StubIngestor(database=database, rows=rows)
    stats = await ingestor.sync()

    assert stats.bookmarks_seen == 7
    assert stats.requests_created == 7
    assert stats.metadata_inserted == 7
    assert stats.skipped_invalid_category == 0

    async with database.session() as verify:
        stored = (await verify.execute(select(FieldTheoryBookmarkMetadata))).scalars().all()
    stored_categories = {meta.fieldtheory_category for meta in stored}
    assert stored_categories == {category.value for category in categories}
