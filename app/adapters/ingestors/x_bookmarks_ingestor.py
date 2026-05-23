"""Field Theory bookmark ingestor (read-only aiosqlite delta-scan into Postgres)."""

from __future__ import annotations

import datetime as dt
import pathlib
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import aiosqlite
from sqlalchemy import select

from app.core.logging_utils import get_logger
from app.core.url_utils import compute_dedupe_hash, normalize_url
from app.db.models.core import XBookmarkMetadata, XCategory, Request
from app.domain.models.request import RequestStatus
from app.domain.models.source import SourceKind

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.session import Database

logger = get_logger(__name__)

_VALID_CATEGORIES = frozenset(member.value for member in XCategory)

_BOOKMARKS_QUERY_ALL = (
    "SELECT id, url, text, author_handle, primary_category, posted_at, synced_at "
    "FROM bookmarks ORDER BY synced_at"
)
_BOOKMARKS_QUERY_DELTA = (
    "SELECT id, url, text, author_handle, primary_category, posted_at, synced_at "
    "FROM bookmarks WHERE synced_at > :after ORDER BY synced_at"
)


@dataclass(frozen=True, slots=True)
class XBookmarkRow:
    """Single row read from ft's ``bookmarks.db`` (read-only).

    Mirrors the subset of columns the ingestor cares about; full column list
    in ``fieldtheory-cli/src/bookmarks-db.ts``.
    """

    bookmark_external_id: str
    url: str
    tweet_text: str | None
    tweet_author: str | None
    primary_category: str | None
    posted_at: dt.datetime | None
    synced_at: dt.datetime


@dataclass(frozen=True, slots=True)
class XIngestStats:
    """Summary of one ``sync()`` invocation."""

    bookmarks_seen: int = 0
    requests_created: int = 0
    metadata_inserted: int = 0
    metadata_updated: int = 0
    skipped_invalid_category: int = 0
    skipped_invalid_url: int = 0
    skipped_metadata_slot_taken: int = 0


@dataclass(frozen=True, slots=True)
class _UpsertOutcome:
    request_created: int = 0
    metadata_inserted: int = 0
    metadata_updated: int = 0
    skipped_metadata_slot_taken: int = 0


class XBookmarksIngestor:
    """Sync ft bookmarks into Postgres as ``requests`` + sidecar metadata rows.

    Per DEC-001 (Option C) this ingestor writes directly to ``requests`` and
    ``x_bookmark_metadata``. It does not invoke the URLProcessor,
    Twitter adapter, scraper chain, or summarizer; ingested rows land in a
    terminal ``RequestStatus.X_IMPORTED`` state.

    Concurrency: ft owns the writer side of ``bookmarks.db``; this class is
    a read-only consumer. The connection is opened with the ``?mode=ro``
    URI flag so it can never contend for a write lock.
    """

    def __init__(
        self,
        *,
        database: Database,
        bookmarks_db_path: pathlib.Path | str,
    ) -> None:
        self._database = database
        self._bookmarks_db_path = pathlib.Path(bookmarks_db_path)

    async def sync(self) -> XIngestStats:
        """Run one delta scan + idempotent upsert pass."""
        async with self._database.session() as session:
            after = await self._latest_synced_at(session)
            seen = 0
            requests_created = 0
            metadata_inserted = 0
            metadata_updated = 0
            skipped_invalid_category = 0
            skipped_invalid_url = 0

            skipped_metadata_slot_taken = 0

            async for row in self._iter_bookmarks(after=after):
                seen += 1

                if row.primary_category not in _VALID_CATEGORIES:
                    skipped_invalid_category += 1
                    logger.debug(
                        "x_bookmarks_ingest_skipped_category",
                        extra={
                            "bookmark_external_id": row.bookmark_external_id,
                            "category": row.primary_category,
                        },
                    )
                    continue

                try:
                    normalized = normalize_url(row.url)
                    dedupe_hash = compute_dedupe_hash(row.url)
                except ValueError:
                    skipped_invalid_url += 1
                    logger.warning(
                        "x_bookmarks_ingest_skipped_url",
                        extra={
                            "bookmark_external_id": row.bookmark_external_id,
                            "url": row.url[:200],
                        },
                    )
                    continue

                outcome = await self._upsert(
                    session,
                    row=row,
                    normalized_url=normalized,
                    dedupe_hash=dedupe_hash,
                )
                requests_created += outcome.request_created
                metadata_inserted += outcome.metadata_inserted
                metadata_updated += outcome.metadata_updated
                skipped_metadata_slot_taken += outcome.skipped_metadata_slot_taken

            await session.commit()

            return XIngestStats(
                bookmarks_seen=seen,
                requests_created=requests_created,
                metadata_inserted=metadata_inserted,
                metadata_updated=metadata_updated,
                skipped_invalid_category=skipped_invalid_category,
                skipped_invalid_url=skipped_invalid_url,
                skipped_metadata_slot_taken=skipped_metadata_slot_taken,
            )

    async def _latest_synced_at(self, session: AsyncSession) -> dt.datetime | None:
        """Watermark: highest ``synced_at`` already mirrored into Postgres."""
        stmt = (
            select(XBookmarkMetadata.synced_at)
            .order_by(XBookmarkMetadata.synced_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _iter_bookmarks(
        self,
        *,
        after: dt.datetime | None,
    ) -> AsyncIterator[XBookmarkRow]:
        """Stream rows from ft's ``bookmarks.db`` newer than the watermark.

        Override in tests to inject deterministic rows without a real SQLite
        file; the production path opens read-only via aiosqlite.
        """
        dsn = f"file:{self._bookmarks_db_path}?mode=ro"
        async with aiosqlite.connect(dsn, uri=True) as conn:
            if after is None:
                cursor = await conn.execute(_BOOKMARKS_QUERY_ALL)
            else:
                cursor = await conn.execute(_BOOKMARKS_QUERY_DELTA, {"after": _format_iso(after)})
            try:
                async for raw in cursor:
                    parsed = _parse_row(raw)
                    if parsed is not None:
                        yield parsed
            finally:
                await cursor.close()

    async def _upsert(
        self,
        session: AsyncSession,
        *,
        row: XBookmarkRow,
        normalized_url: str,
        dedupe_hash: str,
    ) -> _UpsertOutcome:
        # Path C: ft bookmark already known — refresh metadata, leave Request.
        existing_metadata = await session.scalar(
            select(XBookmarkMetadata).where(
                XBookmarkMetadata.bookmark_external_id == row.bookmark_external_id
            )
        )
        if existing_metadata is not None:
            existing_metadata.x_category = row.primary_category or ""
            existing_metadata.tweet_text = row.tweet_text
            existing_metadata.tweet_author = row.tweet_author
            existing_metadata.tweet_url = row.url
            existing_metadata.posted_at = row.posted_at
            existing_metadata.synced_at = row.synced_at
            await session.flush()
            return _UpsertOutcome(metadata_updated=1)

        # Path B: URL was already a Request — attach metadata to it.
        existing_request = await session.scalar(
            select(Request).where(Request.dedupe_hash == dedupe_hash)
        )
        if existing_request is not None:
            # The metadata table is keyed by request_id (one Request : one FT
            # metadata row by design — see docs/explanation/x-bookmarks-integration.md
            # line 62). If a prior FT bookmark already claimed this slot under
            # a different bookmark_external_id (rare: two ft bookmarks whose URLs
            # normalize to the same dedupe_hash), keep the first claim and
            # log the collision. ft itself stores both rows on its side; this
            # is just our 1:1 mirror.
            already_claimed = await session.scalar(
                select(XBookmarkMetadata.bookmark_external_id).where(
                    XBookmarkMetadata.request_id == existing_request.id
                )
            )
            if already_claimed is not None:
                logger.warning(
                    "x_bookmarks_ingest_metadata_slot_taken",
                    extra={
                        "bookmark_external_id": row.bookmark_external_id,
                        "existing_bookmark_external_id": already_claimed,
                        "request_id": existing_request.id,
                    },
                )
                return _UpsertOutcome(skipped_metadata_slot_taken=1)
            metadata = XBookmarkMetadata(
                request_id=existing_request.id,
                bookmark_external_id=row.bookmark_external_id,
                x_category=row.primary_category or "",
                tweet_text=row.tweet_text,
                tweet_author=row.tweet_author,
                tweet_url=row.url,
                posted_at=row.posted_at,
                synced_at=row.synced_at,
            )
            session.add(metadata)
            await session.flush()
            return _UpsertOutcome(metadata_inserted=1)

        # Path A: brand new — INSERT Request with fresh correlation_id, then metadata.
        request = Request(
            type=SourceKind.X_BOOKMARK.value,
            status=RequestStatus.X_IMPORTED.value,
            correlation_id=uuid4().hex,
            input_url=row.url,
            normalized_url=normalized_url,
            dedupe_hash=dedupe_hash,
        )
        session.add(request)
        await session.flush()

        metadata = XBookmarkMetadata(
            request_id=request.id,
            bookmark_external_id=row.bookmark_external_id,
            x_category=row.primary_category or "",
            tweet_text=row.tweet_text,
            tweet_author=row.tweet_author,
            tweet_url=row.url,
            posted_at=row.posted_at,
            synced_at=row.synced_at,
        )
        session.add(metadata)
        await session.flush()
        return _UpsertOutcome(request_created=1, metadata_inserted=1)


def _parse_row(raw: Sequence[object]) -> XBookmarkRow | None:
    """Convert a raw aiosqlite row into a typed row, dropping malformed entries."""
    if len(raw) < 7:
        return None
    bookmark_external_id = raw[0]
    url = raw[1]
    text_value = raw[2]
    author_handle = raw[3]
    primary_category = raw[4]
    posted_at = raw[5]
    synced_at = raw[6]

    if not isinstance(bookmark_external_id, str) or not bookmark_external_id:
        return None
    if not isinstance(url, str) or not url:
        return None
    synced_at_dt = _parse_iso(synced_at)
    if synced_at_dt is None:
        return None

    return XBookmarkRow(
        bookmark_external_id=bookmark_external_id,
        url=url,
        tweet_text=text_value if isinstance(text_value, str) else None,
        tweet_author=author_handle if isinstance(author_handle, str) else None,
        primary_category=primary_category if isinstance(primary_category, str) else None,
        posted_at=_parse_iso(posted_at),
        synced_at=synced_at_dt,
    )


def _parse_iso(value: object) -> dt.datetime | None:
    """Parse an ISO 8601 string from ft into a timezone-aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def _format_iso(value: dt.datetime) -> str:
    """Format a datetime back to an ISO 8601 string for the delta-scan parameter."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat()


__all__ = [
    "XBookmarkRow",
    "XBookmarksIngestor",
    "XIngestStats",
]
