"""SQLAlchemy repository for user-owned goals, digests, highlights, and exports."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select, update

from app.db.models import (
    Collection,
    CollectionItem,
    CustomDigest,
    Request,
    Summary,
    SummaryHighlight,
    SummaryTag,
    Tag,
    UserGoal,
    model_to_dict,
)

if TYPE_CHECKING:
    from app.db.session import Database


class UserContentRepositoryAdapter:
    """Owns database access for user-content features outside the core summary flow."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_list_goals(self, user_id: int) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.execute(select(UserGoal).where(UserGoal.user_id == user_id))
            ).scalars()
            return [model_to_dict(row) or {} for row in rows]

    async def async_upsert_goal(
        self,
        *,
        user_id: int,
        goal_type: str,
        scope_type: str,
        scope_id: int | None,
        target_count: int,
    ) -> dict[str, Any]:
        async with self._database.transaction() as session:
            goal = await session.scalar(
                select(UserGoal).where(
                    UserGoal.user_id == user_id,
                    UserGoal.goal_type == goal_type,
                    UserGoal.scope_type == scope_type,
                    UserGoal.scope_id == scope_id,
                )
            )
            if goal is None:
                goal = UserGoal(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    goal_type=goal_type,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    target_count=target_count,
                )
                session.add(goal)
            else:
                goal.target_count = target_count
            await session.flush()
            return model_to_dict(goal) or {}

    async def async_delete_global_goal(self, *, user_id: int, goal_type: str) -> int:
        async with self._database.transaction() as session:
            deleted_ids = (
                await session.execute(
                    delete(UserGoal)
                    .where(
                        UserGoal.user_id == user_id,
                        UserGoal.goal_type == goal_type,
                        UserGoal.scope_type == "global",
                    )
                    .returning(UserGoal.id)
                )
            ).scalars()
            return len(list(deleted_ids))

    async def async_delete_goal_by_id(self, *, user_id: int, goal_id: str) -> int:
        async with self._database.transaction() as session:
            deleted_ids = (
                await session.execute(
                    delete(UserGoal)
                    .where(UserGoal.user_id == user_id, UserGoal.id == _uuid(goal_id))
                    .returning(UserGoal.id)
                )
            ).scalars()
            return len(list(deleted_ids))

    async def async_get_scope_name(
        self,
        *,
        user_id: int,
        scope_type: str,
        scope_id: int | None,
    ) -> str | None:
        async with self._database.session() as session:
            if scope_type == "tag" and scope_id is not None:
                return await session.scalar(
                    select(Tag.name).where(
                        Tag.id == scope_id,
                        Tag.user_id == user_id,
                        Tag.is_deleted.is_(False),
                    )
                )
            if scope_type == "collection" and scope_id is not None:
                return await session.scalar(
                    select(Collection.name).where(
                        Collection.id == scope_id,
                        Collection.user_id == user_id,
                        Collection.is_deleted.is_(False),
                    )
                )
            return None

    async def async_count_scoped_summaries_in_period(
        self,
        *,
        user_id: int,
        start: Any,
        end: Any,
        scope_type: str,
        scope_id: int | None,
    ) -> int:
        async with self._database.session() as session:
            stmt = (
                select(Summary.id)
                .join(Request, Summary.request_id == Request.id)
                .where(
                    Request.user_id == user_id,
                    Summary.created_at >= start,
                    Summary.created_at < end,
                    Summary.is_deleted.is_(False),
                )
            )
            if scope_type == "tag" and scope_id is not None:
                stmt = stmt.join(SummaryTag, SummaryTag.summary_id == Summary.id).where(
                    SummaryTag.tag_id == scope_id
                )
            elif scope_type == "collection" and scope_id is not None:
                stmt = stmt.join(CollectionItem, CollectionItem.summary_id == Summary.id).where(
                    CollectionItem.collection_id == scope_id
                )
            return len(list((await session.execute(stmt)).scalars()))

    async def async_get_owned_summaries(
        self,
        *,
        user_id: int,
        summary_ids: list[int],
    ) -> list[dict[str, Any]]:
        if not summary_ids:
            return []
        async with self._database.session() as session:
            rows = await session.execute(
                select(Summary, Request)
                .join(Request, Summary.request_id == Request.id)
                .where(
                    Summary.id.in_(summary_ids),
                    Request.user_id == user_id,
                    Summary.is_deleted.is_(False),
                )
            )
            return [_summary_with_request(summary, request) for summary, request in rows]

    async def async_create_custom_digest(
        self,
        *,
        user_id: int,
        title: str,
        summary_ids: list[int],
        format: str,
        content: str,
    ) -> dict[str, Any]:
        async with self._database.transaction() as session:
            digest = CustomDigest(
                id=uuid.uuid4(),
                user_id=user_id,
                title=title,
                summary_ids=json.dumps([str(item) for item in summary_ids]),
                format=format,
                content=content,
                status="ready",
            )
            session.add(digest)
            await session.flush()
            return model_to_dict(digest) or {}

    async def async_list_custom_digests(self, user_id: int) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(CustomDigest)
                    .where(CustomDigest.user_id == user_id)
                    .order_by(CustomDigest.created_at.desc())
                )
            ).scalars()
            return [model_to_dict(row) or {} for row in rows]

    async def async_get_custom_digest(
        self,
        digest_id: str,
        *,
        user_id: int,
    ) -> dict[str, Any] | None:
        async with self._database.session() as session:
            digest = await session.scalar(
                select(CustomDigest).where(
                    CustomDigest.id == _uuid(digest_id),
                    CustomDigest.user_id == user_id,
                )
            )
            return model_to_dict(digest)

    async def async_get_owned_summary(
        self,
        *,
        user_id: int,
        summary_id: int,
    ) -> dict[str, Any] | None:
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(Summary, Request)
                    .join(Request, Summary.request_id == Request.id)
                    .where(Summary.id == summary_id, Request.user_id == user_id)
                )
            ).first()
            if row is None:
                return None
            summary, request = row
            return _summary_with_request(summary, request)

    async def async_list_highlights(
        self,
        *,
        user_id: int,
        summary_id: int,
    ) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(SummaryHighlight)
                    .where(
                        SummaryHighlight.user_id == user_id,
                        SummaryHighlight.summary_id == summary_id,
                    )
                    .order_by(SummaryHighlight.created_at.asc())
                )
            ).scalars()
            return [model_to_dict(row) or {} for row in rows]

    async def async_create_highlight(
        self,
        *,
        user_id: int,
        summary_id: int,
        text: str,
        start_offset: int,
        end_offset: int,
        color: str | None,
        note: str | None,
    ) -> dict[str, Any]:
        async with self._database.transaction() as session:
            highlight = SummaryHighlight(
                id=uuid.uuid4(),
                user_id=user_id,
                summary_id=summary_id,
                text=text,
                start_offset=start_offset,
                end_offset=end_offset,
                color=color,
                note=note,
            )
            session.add(highlight)
            await session.flush()
            return model_to_dict(highlight) or {}

    async def async_get_highlight(
        self,
        *,
        user_id: int,
        summary_id: int,
        highlight_id: str,
    ) -> dict[str, Any] | None:
        async with self._database.session() as session:
            highlight = await session.scalar(
                select(SummaryHighlight).where(
                    SummaryHighlight.id == _uuid(highlight_id),
                    SummaryHighlight.user_id == user_id,
                    SummaryHighlight.summary_id == summary_id,
                )
            )
            return model_to_dict(highlight)

    async def async_update_highlight(
        self,
        *,
        user_id: int,
        highlight_id: str,
        color: str | None,
        note: str | None,
    ) -> dict[str, Any]:
        update_fields: dict[str, Any] = {}
        if color is not None:
            update_fields["color"] = color
        if note is not None:
            update_fields["note"] = note

        async with self._database.transaction() as session:
            if update_fields:
                await session.execute(
                    update(SummaryHighlight)
                    .where(
                        SummaryHighlight.id == _uuid(highlight_id),
                        SummaryHighlight.user_id == user_id,
                    )
                    .values(**update_fields)
                )
            highlight = await session.scalar(
                select(SummaryHighlight).where(
                    SummaryHighlight.id == _uuid(highlight_id),
                    SummaryHighlight.user_id == user_id,
                )
            )
            return model_to_dict(highlight) or {}

    async def async_delete_highlight(self, *, user_id: int, highlight_id: str) -> None:
        async with self._database.transaction() as session:
            await session.execute(
                delete(SummaryHighlight).where(
                    SummaryHighlight.id == _uuid(highlight_id),
                    SummaryHighlight.user_id == user_id,
                )
            )

    async def async_export_summaries(
        self,
        *,
        user_id: int,
        tag: str | None,
        collection_id: int | None,
    ) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            stmt = (
                select(Summary, Request)
                .join(Request, Summary.request_id == Request.id)
                .where(Request.user_id == user_id, Summary.is_deleted.is_(False))
            )
            if tag:
                tag_id = await session.scalar(
                    select(Tag.id).where(
                        Tag.user_id == user_id,
                        Tag.name == tag,
                        Tag.is_deleted.is_(False),
                    )
                )
                if tag_id is None:
                    return []
                stmt = stmt.join(SummaryTag, SummaryTag.summary_id == Summary.id).where(
                    SummaryTag.tag_id == tag_id
                )
            if collection_id is not None:
                stmt = (
                    stmt.join(CollectionItem, CollectionItem.summary_id == Summary.id)
                    .join(Collection, Collection.id == CollectionItem.collection_id)
                    .where(
                        CollectionItem.collection_id == collection_id,
                        Collection.user_id == user_id,
                        Collection.is_deleted.is_(False),
                    )
                )

            rows = await session.execute(stmt)
            summaries: list[dict[str, Any]] = []
            for summary, request in rows:
                summary_dict = model_to_dict(summary)
                if summary_dict is None:
                    continue
                summary_dict["url"] = request.input_url or request.normalized_url or ""
                summary_dict["title"] = ""
                json_payload = summary_dict.get("json_payload")
                if isinstance(json_payload, dict):
                    summary_dict["title"] = json_payload.get("title", "")
                summary_dict["tags"] = await _summary_tags(session, summary.id, user_id=user_id)
                summary_dict["collections"] = await _summary_collections(
                    session, summary.id, user_id=user_id
                )
                summaries.append(summary_dict)
            return summaries


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _summary_with_request(summary: Summary, request: Request) -> dict[str, Any]:
    item = model_to_dict(summary) or {}
    item["request"] = model_to_dict(request) or {}
    return item


async def _summary_tags(session: Any, summary_id: int, *, user_id: int) -> list[dict[str, str]]:
    rows = await session.execute(
        select(Tag.name)
        .join(SummaryTag, SummaryTag.tag_id == Tag.id)
        .where(
            SummaryTag.summary_id == summary_id,
            Tag.user_id == user_id,
            Tag.is_deleted.is_(False),
        )
        .order_by(Tag.name.asc())
    )
    return [{"name": name} for name in rows.scalars()]


async def _summary_collections(
    session: Any, summary_id: int, *, user_id: int
) -> list[dict[str, str]]:
    rows = await session.execute(
        select(Collection.name)
        .join(CollectionItem, CollectionItem.collection_id == Collection.id)
        .where(
            CollectionItem.summary_id == summary_id,
            Collection.user_id == user_id,
            Collection.is_deleted.is_(False),
        )
        .order_by(Collection.name.asc())
    )
    return [{"name": name} for name in rows.scalars()]
