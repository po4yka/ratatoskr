"""Admin read queries for users and their aggregate content counts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.db.models import Collection, Request, Summary, Tag, User
from app.infrastructure.persistence.repositories.admin._helpers import isotime

if TYPE_CHECKING:
    from app.db.session import Database


class UsersReadRepository:
    """Read-side queries for the users bounded context."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_list_users(self) -> dict[str, Any]:
        async with self._database.session() as session:
            summary_counts = (
                select(
                    Request.user_id.label("user_id"),
                    func.count(Summary.id).label("summary_count"),
                )
                .join(Summary, Summary.request_id == Request.id)
                .group_by(Request.user_id)
                .subquery()
            )
            request_counts = (
                select(
                    Request.user_id.label("user_id"),
                    func.count(Request.id).label("request_count"),
                )
                .group_by(Request.user_id)
                .subquery()
            )
            tag_counts = (
                select(
                    Tag.user_id.label("user_id"),
                    func.count(Tag.id).label("tag_count"),
                )
                .group_by(Tag.user_id)
                .subquery()
            )
            collection_counts = (
                select(
                    Collection.user_id.label("user_id"),
                    func.count(Collection.id).label("collection_count"),
                )
                .group_by(Collection.user_id)
                .subquery()
            )

            rows = (
                await session.execute(
                    select(
                        User.telegram_user_id,
                        User.username,
                        User.is_owner,
                        User.created_at,
                        func.coalesce(summary_counts.c.summary_count, 0),
                        func.coalesce(request_counts.c.request_count, 0),
                        func.coalesce(tag_counts.c.tag_count, 0),
                        func.coalesce(collection_counts.c.collection_count, 0),
                    )
                    .outerjoin(
                        summary_counts,
                        summary_counts.c.user_id == User.telegram_user_id,
                    )
                    .outerjoin(
                        request_counts,
                        request_counts.c.user_id == User.telegram_user_id,
                    )
                    .outerjoin(tag_counts, tag_counts.c.user_id == User.telegram_user_id)
                    .outerjoin(
                        collection_counts,
                        collection_counts.c.user_id == User.telegram_user_id,
                    )
                    .order_by(User.created_at.asc())
                )
            ).all()
            users_list: list[dict[str, Any]] = []
            for (
                uid,
                username,
                is_owner,
                created_at,
                summary_count,
                request_count,
                tag_count,
                collection_count,
            ) in rows:
                users_list.append(
                    {
                        "user_id": uid,
                        "username": username,
                        "is_owner": is_owner,
                        "summary_count": int(summary_count or 0),
                        "request_count": int(request_count or 0),
                        "tag_count": int(tag_count or 0),
                        "collection_count": int(collection_count or 0),
                        "created_at": isotime(created_at),
                    }
                )
            return {"users": users_list, "total_users": len(users_list)}
