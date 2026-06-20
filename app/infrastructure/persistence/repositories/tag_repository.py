"""SQLAlchemy implementation of the tag repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Request, Summary, SummaryTag, Tag, model_to_dict
from app.db.types import _utcnow

if TYPE_CHECKING:
    from app.db.session import Database


class TagRepositoryAdapter:
    """Adapter for tag CRUD and summary-tag association operations."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_get_user_tags(self, user_id: int) -> list[dict[str, Any]]:
        """Return all non-deleted tags owned by a user, with summary counts."""
        async with self._database.session() as session:
            count_expr = func.count(SummaryTag.id).label("summary_count")
            rows = await session.execute(
                select(Tag, count_expr)
                .outerjoin(SummaryTag, SummaryTag.tag_id == Tag.id)
                .where(Tag.user_id == user_id, Tag.is_deleted.is_(False))
                .group_by(Tag.id)
                .order_by(Tag.created_at.asc())
            )
            result: list[dict[str, Any]] = []
            for tag, summary_count in rows:
                data = model_to_dict(tag) or {}
                data["summary_count"] = int(summary_count or 0)
                result.append(data)
            return result

    async def async_get_tag_by_id(self, tag_id: int) -> dict[str, Any] | None:
        """Return tag by ID."""
        async with self._database.session() as session:
            tag = await session.get(Tag, tag_id)
            return model_to_dict(tag)

    async def async_create_tag(
        self,
        user_id: int,
        name: str,
        normalized_name: str,
        color: str | None,
    ) -> dict[str, Any]:
        """Create a tag and return the created record."""
        async with self._database.transaction() as session:
            tag = Tag(
                user_id=user_id,
                name=name,
                normalized_name=normalized_name,
                color=color,
            )
            session.add(tag)
            await session.flush()
            data = model_to_dict(tag) or {}
            data["summary_count"] = 0
            return data

    async def async_update_tag(
        self,
        tag_id: int,
        name: str | None,
        color: str | None,
        *,
        user_id: int,
    ) -> dict[str, Any]:
        """Update a tag and return the updated record."""
        update_values: dict[str, Any] = {"updated_at": _utcnow()}
        if name is not None:
            from app.domain.services.tag_service import normalize_tag_name

            update_values["name"] = name
            update_values["normalized_name"] = normalize_tag_name(name)
        if color is not None:
            update_values["color"] = color

        async with self._database.transaction() as session:
            await session.execute(
                update(Tag).where(Tag.id == tag_id, Tag.user_id == user_id).values(**update_values)
            )
            tag = await session.get(Tag, tag_id)
            return model_to_dict(tag) or {}

    async def async_delete_tag(self, tag_id: int, *, user_id: int) -> None:
        """Soft-delete a tag."""
        async with self._database.transaction() as session:
            await session.execute(
                update(Tag)
                .where(Tag.id == tag_id, Tag.user_id == user_id)
                .values(is_deleted=True, deleted_at=_utcnow(), updated_at=_utcnow())
            )

    async def async_attach_tag(
        self,
        summary_id: int,
        tag_id: int,
        source: str,
    ) -> dict[str, Any]:
        """Attach a tag to a summary. Ignore if already exists."""
        async with self._database.transaction() as session:
            stmt = (
                insert(SummaryTag)
                .values(summary_id=summary_id, tag_id=tag_id, source=source)
                .on_conflict_do_nothing(index_elements=[SummaryTag.summary_id, SummaryTag.tag_id])
                .returning(SummaryTag)
            )
            association = await session.scalar(stmt)
            if association is None:
                association = await session.scalar(
                    select(SummaryTag).where(
                        SummaryTag.summary_id == summary_id,
                        SummaryTag.tag_id == tag_id,
                    )
                )
            return model_to_dict(association) or {}

    async def async_detach_tag(self, summary_id: int, tag_id: int) -> None:
        """Detach a tag from a summary."""
        async with self._database.transaction() as session:
            await session.execute(
                delete(SummaryTag).where(
                    SummaryTag.summary_id == summary_id,
                    SummaryTag.tag_id == tag_id,
                )
            )

    async def async_restore_tag(
        self, tag_id: int, *, user_id: int, name: str | None = None
    ) -> dict[str, Any]:
        """Restore a previously soft-deleted tag."""
        update_values: dict[str, Any] = {
            "is_deleted": False,
            "deleted_at": None,
            "updated_at": _utcnow(),
        }
        if name is not None:
            update_values["name"] = name
        async with self._database.transaction() as session:
            await session.execute(
                update(Tag).where(Tag.id == tag_id, Tag.user_id == user_id).values(**update_values)
            )
            tag = await session.get(Tag, tag_id)
            return model_to_dict(tag) or {}

    async def async_get_tags_for_summary(self, summary_id: int) -> list[dict[str, Any]]:
        """Return all tags attached to a summary with source info."""
        async with self._database.session() as session:
            rows = await session.execute(
                select(Tag, SummaryTag)
                .join(SummaryTag, SummaryTag.tag_id == Tag.id)
                .where(SummaryTag.summary_id == summary_id, Tag.is_deleted.is_(False))
                .order_by(SummaryTag.created_at.asc())
            )
            result: list[dict[str, Any]] = []
            for tag, summary_tag in rows:
                data = model_to_dict(tag) or {}
                data["source"] = summary_tag.source
                data["attached_at"] = summary_tag.created_at
                result.append(data)
            return result

    async def async_get_tagged_summaries(
        self,
        *,
        user_id: int,
        tag_id: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return recent summaries for a tag owned by the user."""
        async with self._database.session() as session:
            rows = await session.execute(
                select(Summary, Request)
                .join(SummaryTag, SummaryTag.summary_id == Summary.id)
                .join(Request, Summary.request_id == Request.id)
                .where(
                    SummaryTag.tag_id == tag_id,
                    Request.user_id == user_id,
                    Summary.is_deleted.is_(False),
                )
                .order_by(Summary.created_at.desc())
                .limit(limit)
            )
            result: list[dict[str, Any]] = []
            for summary, request in rows:
                data = model_to_dict(summary) or {}
                data["request"] = model_to_dict(request)
                result.append(data)
            return result

    async def async_merge_tags(
        self, source_tag_ids: list[int], target_tag_id: int, *, user_id: int
    ) -> None:
        """Merge source tags into target: re-point associations, soft-delete sources.

        Both source and target tags must belong to user_id; the WHERE clauses
        enforce ownership as a defense-in-depth IDOR guard.
        """
        if not source_tag_ids:
            return
        async with self._database.transaction() as session:
            existing_summary_ids = set(
                await session.scalars(
                    select(SummaryTag.summary_id).where(
                        SummaryTag.tag_id == target_tag_id,
                    )
                )
            )
            for src_id in source_tag_ids:
                source_rows = (
                    await session.execute(select(SummaryTag).where(SummaryTag.tag_id == src_id))
                ).scalars()
                for source_row in source_rows:
                    if source_row.summary_id not in existing_summary_ids:
                        source_row.tag_id = target_tag_id
                        existing_summary_ids.add(source_row.summary_id)
                    else:
                        await session.delete(source_row)

            await session.execute(
                update(Tag)
                .where(Tag.id.in_(source_tag_ids), Tag.user_id == user_id)
                .values(is_deleted=True, deleted_at=_utcnow(), updated_at=_utcnow())
            )

    async def async_find_or_create_tag(
        self,
        user_id: int,
        name: str,
        normalized_name: str,
        color: str | None,
    ) -> dict[str, Any]:
        """Atomically find or create a tag by normalized name.

        Uses INSERT ... ON CONFLICT DO NOTHING so concurrent requests for the
        same (user_id, normalized_name) pair are race-safe.  The unique index
        ``ix_tags_user_id_normalized_name`` is the conflict target.
        """
        async with self._database.transaction() as session:
            stmt = (
                insert(Tag)
                .values(
                    user_id=user_id,
                    name=name,
                    normalized_name=normalized_name,
                    color=color,
                )
                .on_conflict_do_nothing(
                    index_elements=["user_id", "normalized_name"],
                )
                .returning(Tag)
            )
            tag = await session.scalar(stmt)
            if tag is None:
                # Conflict path: the row already existed; re-fetch it.
                tag = await session.scalar(
                    select(Tag).where(
                        Tag.user_id == user_id,
                        Tag.normalized_name == normalized_name,
                        Tag.is_deleted.is_(False),
                    )
                )
            data = model_to_dict(tag) or {}
            data.setdefault("summary_count", 0)
            return data

    async def async_get_tag_by_normalized_name(
        self,
        user_id: int,
        normalized_name: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        """Return tag by normalized name within a user scope."""
        async with self._database.session() as session:
            stmt = select(Tag).where(Tag.user_id == user_id, Tag.normalized_name == normalized_name)
            if not include_deleted:
                stmt = stmt.where(Tag.is_deleted.is_(False))
            tag = await session.scalar(stmt)
            return model_to_dict(tag)
