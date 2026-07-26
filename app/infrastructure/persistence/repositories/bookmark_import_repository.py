"""Transactional SQLAlchemy bookmark import adapter."""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.application.dto.import_bookmarks import BookmarkImportItemResult
from app.core.time_utils import UTC
from app.core.url_utils import compute_dedupe_hash, normalize_url
from app.db.models import Collection, CollectionItem, Request, Summary, SummaryTag, Tag
from app.domain.services.tag_service import normalize_tag_name

if TYPE_CHECKING:
    from app.db.session import Database
    from app.domain.services.import_parsers.base import ImportedBookmark


class BookmarkImportAdapter:
    """Import one bookmark and all derived records in a single transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_import_bookmark(
        self,
        bookmark: ImportedBookmark,
        *,
        user_id: int,
        options: dict[str, Any],
    ) -> BookmarkImportItemResult:
        """Persist a bookmark and its linked summary/tag records."""

        now = _dt.datetime.now(UTC)
        created_at = bookmark.created_at or now
        normalized_url = normalize_url(bookmark.url)
        dedupe_hash = compute_dedupe_hash(normalized_url)
        server_version = int(now.timestamp() * 1000)

        async with self._database.transaction() as session:
            existing_request_id = await session.scalar(
                select(Request.id).where(Request.dedupe_hash == dedupe_hash)
            )
            if existing_request_id is not None:
                return BookmarkImportItemResult(url=bookmark.url, outcome="skipped")

            request = Request(
                type="import",
                status="completed",
                user_id=user_id,
                input_url=bookmark.url,
                normalized_url=normalized_url,
                dedupe_hash=dedupe_hash,
                content_text=bookmark.notes,
                created_at=created_at,
                updated_at=now,
                server_version=server_version,
            )
            session.add(request)
            await session.flush()

            summary = Summary(
                request_id=request.id,
                json_payload={
                    "title": bookmark.title or bookmark.url,
                    "summary_250": bookmark.notes or "",
                    "topic_tags": bookmark.tags,
                },
                lang=None,
                server_version=server_version,
                created_at=created_at,
                updated_at=now,
            )
            session.add(summary)
            await session.flush()

            if options.get("create_tags", True):
                for raw_tag in bookmark.tags:
                    normalized = normalize_tag_name(raw_tag)
                    if not normalized:
                        continue
                    tag_id = await session.scalar(
                        insert(Tag)
                        .values(
                            user_id=user_id,
                            normalized_name=normalized,
                            name=raw_tag.strip(),
                        )
                        .on_conflict_do_nothing(index_elements=[Tag.user_id, Tag.normalized_name])
                        .returning(Tag.id)
                    )
                    if tag_id is None:
                        tag_id = await session.scalar(
                            select(Tag.id).where(
                                Tag.user_id == user_id,
                                Tag.normalized_name == normalized,
                            )
                        )
                    if tag_id is None:
                        msg = f"failed to resolve tag {normalized!r}"
                        raise RuntimeError(msg)
                    await session.execute(
                        insert(SummaryTag)
                        .values(summary_id=summary.id, tag_id=tag_id, source="import")
                        .on_conflict_do_nothing(
                            index_elements=[SummaryTag.summary_id, SummaryTag.tag_id]
                        )
                    )

            target_collection_id = options.get("target_collection_id")
            if target_collection_id is not None:
                collection_id = await session.scalar(
                    select(Collection.id).where(
                        Collection.id == target_collection_id,
                        Collection.user_id == user_id,
                    )
                )
                if collection_id is None:
                    msg = f"collection {target_collection_id} not found or not owned by user"
                    raise ValueError(msg)
                await session.execute(
                    insert(CollectionItem)
                    .values(collection_id=collection_id, summary_id=summary.id)
                    .on_conflict_do_nothing(
                        index_elements=[CollectionItem.collection_id, CollectionItem.summary_id]
                    )
                )

            return BookmarkImportItemResult(url=bookmark.url, outcome="created")
