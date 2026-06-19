"""SQLAlchemy implementation of the collection repository adapter."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.core.time_utils import UTC, coerce_datetime
from app.db.models import (
    AuditLog,
    Collection,
    CollectionCollaborator,
    CollectionInvite,
    CollectionItem,
    CollectionPublicLink,
    Request,
    Summary,
    User,
    model_to_dict,
)

if TYPE_CHECKING:
    from app.db.session import Database


def _now() -> dt.datetime:
    return dt.datetime.now(UTC)


class CollectionRepositoryAdapter:
    """Public collection repository adapter."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_get_collection(
        self, collection_id: int, *, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        async with self._database.session() as session:
            stmt = select(Collection).where(Collection.id == collection_id)
            if not include_deleted:
                stmt = stmt.where(Collection.is_deleted.is_(False))
            collection = await session.scalar(stmt)
            return await _serialize_collection(session, collection)

    async def async_list_collections(
        self,
        user_id: int,
        parent_id: int | None,
        limit: int,
        offset: int,
        membership: str = "any",
    ) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            collab_ids = select(CollectionCollaborator.collection_id).where(
                CollectionCollaborator.user_id == user_id,
                CollectionCollaborator.status == "active",
            )
            stmt = (
                select(Collection)
                .where(Collection.is_deleted.is_(False))
                .order_by(Collection.position.asc(), Collection.created_at.asc())
                .limit(limit)
                .offset(offset)
            )
            if membership == "owned":
                stmt = stmt.where(Collection.user_id == user_id)
            elif membership == "shared":
                stmt = stmt.where(
                    Collection.id.in_(collab_ids),
                    Collection.user_id != user_id,
                )
            else:
                stmt = stmt.where(or_(Collection.user_id == user_id, Collection.id.in_(collab_ids)))
            if parent_id is None:
                stmt = stmt.where(Collection.parent_id.is_(None))
            else:
                stmt = stmt.where(Collection.parent_id == parent_id)
            rows = (await session.execute(stmt)).scalars().all()
            return await _serialize_collections(session, list(rows))

    async def async_create_collection(
        self,
        *,
        user_id: int,
        name: str,
        description: str | None,
        parent_id: int | None,
        position: int,
        collection_type: str = "manual",
        query_conditions_json: list[dict[str, Any]] | None = None,
        query_match_mode: str = "all",
    ) -> int:
        async with self._database.transaction() as session:
            if parent_id is not None and await _active_collection(session, parent_id) is None:
                msg = f"parent collection {parent_id} not found"
                raise ValueError(msg)
            collection = Collection(
                user_id=user_id,
                name=name,
                description=description,
                parent_id=parent_id,
                position=position,
                collection_type=collection_type,
                query_conditions_json=query_conditions_json,
                query_match_mode=query_match_mode,
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(collection)
            await session.flush()
            return collection.id

    async def async_update_collection(
        self,
        collection_id: int,
        **fields: Any,
    ) -> None:
        allowed = set(Collection.__table__.columns.keys()) - {"id", "user_id", "created_at"}
        update_fields = {key: value for key, value in fields.items() if key in allowed}
        if not update_fields:
            return
        update_fields["updated_at"] = _now()
        async with self._database.transaction() as session:
            await session.execute(
                update(Collection)
                .where(Collection.id == collection_id, Collection.is_deleted.is_(False))
                .values(**update_fields)
            )

    async def async_soft_delete_collection(self, collection_id: int) -> None:
        async with self._database.transaction() as session:
            await session.execute(
                update(Collection)
                .where(Collection.id == collection_id, Collection.is_deleted.is_(False))
                .values(is_deleted=True, deleted_at=_now(), updated_at=_now())
            )

    async def async_get_next_position(self, parent_id: int | None) -> int:
        async with self._database.session() as session:
            stmt = select(func.max(Collection.position)).where(Collection.is_deleted.is_(False))
            if parent_id is None:
                stmt = stmt.where(Collection.parent_id.is_(None))
            else:
                stmt = stmt.where(Collection.parent_id == parent_id)
            max_pos = await session.scalar(stmt)
            return int(max_pos or 0) + 1

    async def async_shift_positions(self, parent_id: int | None, start: int) -> None:
        async with self._database.transaction() as session:
            stmt = (
                update(Collection)
                .where(Collection.position.is_not(None), Collection.position >= start)
                .values(position=Collection.position + 1)
            )
            if parent_id is None:
                stmt = stmt.where(Collection.parent_id.is_(None))
            else:
                stmt = stmt.where(Collection.parent_id == parent_id)
            await session.execute(stmt)

    async def async_get_collection_tree(self, user_id: int) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            collab_ids = select(CollectionCollaborator.collection_id).where(
                CollectionCollaborator.user_id == user_id,
                CollectionCollaborator.status == "active",
            )
            rows = (
                await session.execute(
                    select(Collection)
                    .where(
                        Collection.is_deleted.is_(False),
                        or_(Collection.user_id == user_id, Collection.id.in_(collab_ids)),
                    )
                    .order_by(
                        Collection.parent_id.asc().nulls_first(),
                        Collection.position.asc(),
                        Collection.created_at.asc(),
                    )
                )
            ).scalars()
            return await _serialize_collections(session, list(rows))

    async def async_reorder_collections(
        self,
        parent_id: int | None,
        item_positions: list[dict[str, int]],
    ) -> None:
        collection_ids = [item["collection_id"] for item in item_positions]
        if not collection_ids:
            return
        async with self._database.transaction() as session:
            stmt = select(Collection.id).where(
                Collection.id.in_(collection_ids),
                Collection.is_deleted.is_(False),
            )
            if parent_id is None:
                stmt = stmt.where(Collection.parent_id.is_(None))
            else:
                stmt = stmt.where(Collection.parent_id == parent_id)
            existing = set((await session.execute(stmt)).scalars())
            positions_by_id = {
                item["collection_id"]: item["position"]
                for item in item_positions
                if item["collection_id"] in existing
            }
            if not positions_by_id:
                return
            await session.execute(
                update(Collection)
                .where(Collection.id.in_(positions_by_id))
                .values(
                    position=case(
                        positions_by_id,
                        value=Collection.id,
                        else_=Collection.position,
                    ),
                    updated_at=_now(),
                )
            )

    async def async_move_collection(
        self,
        collection_id: int,
        parent_id: int | None,
        position: int,
    ) -> dict[str, Any] | None:
        async with self._database.transaction() as session:
            collection = await _active_collection(session, collection_id)
            if collection is None:
                return None
            if parent_id is not None:
                new_parent = await _active_collection(session, parent_id)
                if new_parent is None:
                    return None
                ancestor = new_parent
                while ancestor is not None:
                    if ancestor.id == collection.id:
                        return None
                    ancestor = (
                        await _active_collection(session, ancestor.parent_id)
                        if ancestor.parent_id is not None
                        else None
                    )

            shift = (
                update(Collection)
                .where(Collection.position.is_not(None), Collection.position >= position)
                .values(position=Collection.position + 1)
            )
            if parent_id is None:
                shift = shift.where(Collection.parent_id.is_(None))
            else:
                shift = shift.where(Collection.parent_id == parent_id)
            await session.execute(shift)
            collection.parent_id = parent_id
            collection.position = position
            collection.updated_at = _now()
            await session.flush()
            return _collection_dict(collection)

    async def async_get_item_count(self, collection_id: int) -> int:
        async with self._database.session() as session:
            return await _item_count(session, collection_id)

    async def async_summary_belongs_to_user(self, summary_id: int, user_id: int) -> bool:
        async with self._database.session() as session:
            owned_summary_id = await session.scalar(
                select(Summary.id)
                .join(Request, Summary.request_id == Request.id)
                .where(
                    Summary.id == summary_id,
                    Request.user_id == user_id,
                    Summary.is_deleted.is_(False),
                )
            )
            return owned_summary_id is not None

    async def async_add_item(
        self,
        collection_id: int,
        summary_id: int,
        position: int,
    ) -> bool:
        async with self._database.transaction() as session:
            if await _active_collection(session, collection_id) is None:
                return False
            if await session.get(Summary, summary_id) is None:
                return False
            inserted_id = await session.scalar(
                insert(CollectionItem)
                .values(
                    collection_id=collection_id,
                    summary_id=summary_id,
                    position=position,
                    created_at=_now(),
                )
                .on_conflict_do_nothing(
                    index_elements=[CollectionItem.collection_id, CollectionItem.summary_id]
                )
                .returning(CollectionItem.id)
            )
            if inserted_id is None:
                return False
            await _touch_collection(session, collection_id)
            return True

    async def async_remove_item(self, collection_id: int, summary_id: int) -> None:
        async with self._database.transaction() as session:
            if await _active_collection(session, collection_id) is None:
                return
            await session.execute(
                delete(CollectionItem).where(
                    CollectionItem.collection_id == collection_id,
                    CollectionItem.summary_id == summary_id,
                )
            )
            await _touch_collection(session, collection_id)

    async def async_list_items(
        self,
        collection_id: int,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(CollectionItem)
                    .where(CollectionItem.collection_id == collection_id)
                    .order_by(CollectionItem.position.asc(), CollectionItem.created_at.asc())
                    .limit(limit)
                    .offset(offset)
                )
            ).scalars()
            return [_item_dict(item) for item in rows]

    async def async_list_item_summary_ids(
        self,
        collection_id: int,
        summary_ids: list[int],
    ) -> list[int]:
        if not summary_ids:
            return []
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(CollectionItem.summary_id).where(
                        CollectionItem.collection_id == collection_id,
                        CollectionItem.summary_id.in_(summary_ids),
                    )
                )
            ).scalars()
            return list(rows)

    async def async_get_next_item_position(self, collection_id: int) -> int:
        async with self._database.session() as session:
            max_pos = await session.scalar(
                select(func.max(CollectionItem.position)).where(
                    CollectionItem.collection_id == collection_id
                )
            )
            return int(max_pos or 0) + 1

    async def async_shift_item_positions(self, collection_id: int, start: int) -> None:
        async with self._database.transaction() as session:
            await session.execute(
                update(CollectionItem)
                .where(
                    CollectionItem.collection_id == collection_id,
                    CollectionItem.position.is_not(None),
                    CollectionItem.position >= start,
                )
                .values(position=CollectionItem.position + 1)
            )

    async def async_reorder_items(
        self, collection_id: int, item_positions: list[dict[str, int]]
    ) -> None:
        summary_ids = [item["summary_id"] for item in item_positions]
        async with self._database.transaction() as session:
            if await _active_collection(session, collection_id) is None:
                return
            existing = set(
                (
                    await session.execute(
                        select(CollectionItem.summary_id).where(
                            CollectionItem.collection_id == collection_id,
                            CollectionItem.summary_id.in_(summary_ids),
                        )
                    )
                ).scalars()
            )
            positions_by_summary = {
                item["summary_id"]: item["position"]
                for item in item_positions
                if item["summary_id"] in existing
            }
            if not positions_by_summary:
                return
            # Single CASE-expression bulk UPDATE instead of one UPDATE per item
            # (mirrors async_reorder_collections).
            await session.execute(
                update(CollectionItem)
                .where(
                    CollectionItem.collection_id == collection_id,
                    CollectionItem.summary_id.in_(positions_by_summary),
                )
                .values(
                    position=case(
                        positions_by_summary,
                        value=CollectionItem.summary_id,
                        else_=CollectionItem.position,
                    )
                )
            )
            await _touch_collection(session, collection_id)

    async def async_bulk_set_items(self, collection_id: int, summary_ids: list[int]) -> int:
        async with self._database.transaction() as session:
            if await _active_collection(session, collection_id) is None:
                return 0
            await session.execute(
                delete(CollectionItem).where(CollectionItem.collection_id == collection_id)
            )
            existing_summary_ids = set(
                (
                    await session.execute(select(Summary.id).where(Summary.id.in_(summary_ids)))
                ).scalars()
            )
            values: list[dict[str, Any]] = []
            seen_summary_ids: set[int] = set()
            for position, summary_id in enumerate(summary_ids, start=1):
                if summary_id not in existing_summary_ids or summary_id in seen_summary_ids:
                    continue
                seen_summary_ids.add(summary_id)
                values.append(
                    {
                        "collection_id": collection_id,
                        "summary_id": summary_id,
                        "position": position,
                        "created_at": _now(),
                    }
                )
            inserted = 0
            if values:
                result = await session.execute(
                    insert(CollectionItem)
                    .values(values)
                    .on_conflict_do_nothing(
                        index_elements=[CollectionItem.collection_id, CollectionItem.summary_id]
                    )
                    .returning(CollectionItem.id)
                )
                inserted = len(result.scalars().all())
            await _touch_collection(session, collection_id)
            return inserted

    async def async_move_items(
        self,
        source_collection_id: int,
        target_collection_id: int,
        summary_ids: list[int],
        position: int | None,
    ) -> list[int]:
        async with self._database.transaction() as session:
            if await _active_collection(session, source_collection_id) is None:
                return []
            if await _active_collection(session, target_collection_id) is None:
                return []
            insert_pos = position
            if insert_pos is None:
                max_pos = await session.scalar(
                    select(func.max(CollectionItem.position)).where(
                        CollectionItem.collection_id == target_collection_id
                    )
                )
                insert_pos = int(max_pos or 0) + 1
            existing_summary_ids = set(
                (
                    await session.execute(
                        select(CollectionItem.summary_id).where(
                            CollectionItem.collection_id == source_collection_id,
                            CollectionItem.summary_id.in_(summary_ids),
                        )
                    )
                ).scalars()
            )
            moving_summary_ids = [
                summary_id for summary_id in summary_ids if summary_id in existing_summary_ids
            ]
            moved: list[int] = []
            if moving_summary_ids:
                unique_moving_ids = list(dict.fromkeys(moving_summary_ids))
                await session.execute(
                    delete(CollectionItem).where(
                        CollectionItem.collection_id == source_collection_id,
                        CollectionItem.summary_id.in_(unique_moving_ids),
                    )
                )
                if position is not None:
                    await session.execute(
                        update(CollectionItem)
                        .where(
                            CollectionItem.collection_id == target_collection_id,
                            CollectionItem.position.is_not(None),
                            CollectionItem.position >= insert_pos,
                        )
                        .values(position=CollectionItem.position + len(moving_summary_ids))
                    )

                values: list[dict[str, Any]] = []
                if source_collection_id == target_collection_id:
                    final_positions: dict[int, int] = {}
                    for offset, summary_id in enumerate(moving_summary_ids):
                        final_positions[summary_id] = insert_pos + offset
                    values = [
                        {
                            "collection_id": target_collection_id,
                            "summary_id": summary_id,
                            "position": final_positions[summary_id],
                            "created_at": _now(),
                        }
                        for summary_id in dict.fromkeys(moving_summary_ids)
                    ]
                    moved = moving_summary_ids
                else:
                    target_summary_ids = set(
                        (
                            await session.execute(
                                select(CollectionItem.summary_id).where(
                                    CollectionItem.collection_id == target_collection_id,
                                    CollectionItem.summary_id.in_(unique_moving_ids),
                                )
                            )
                        ).scalars()
                    )
                    seen_target_ids = set(target_summary_ids)
                    insert_order: list[int] = []
                    for summary_id in moving_summary_ids:
                        if summary_id in seen_target_ids:
                            continue
                        seen_target_ids.add(summary_id)
                        insert_order.append(summary_id)
                        values.append(
                            {
                                "collection_id": target_collection_id,
                                "summary_id": summary_id,
                                "position": insert_pos + len(insert_order) - 1,
                                "created_at": _now(),
                            }
                        )
                if values:
                    result = await session.execute(
                        insert(CollectionItem)
                        .values(values)
                        .on_conflict_do_nothing(
                            index_elements=[
                                CollectionItem.collection_id,
                                CollectionItem.summary_id,
                            ]
                        )
                        .returning(CollectionItem.summary_id)
                    )
                    inserted_ids = set(result.scalars().all())
                    if source_collection_id != target_collection_id:
                        moved = [
                            value["summary_id"]
                            for value in values
                            if value["summary_id"] in inserted_ids
                        ]

            await _touch_collections(session, [source_collection_id, target_collection_id])
            return moved

    async def async_get_role(self, collection_id: int, user_id: int) -> str | None:
        async with self._database.session() as session:
            collection = await _active_collection(session, collection_id)
            if collection is None:
                return None
            if collection.user_id == user_id:
                return "owner"
            return await session.scalar(
                select(CollectionCollaborator.role).where(
                    CollectionCollaborator.collection_id == collection_id,
                    CollectionCollaborator.user_id == user_id,
                    CollectionCollaborator.status == "active",
                )
            )

    async def async_add_collaborator(
        self,
        collection_id: int,
        target_user_id: int,
        role: str,
        invited_by: int | None,
    ) -> None:
        async with self._database.transaction() as session:
            collection = await _active_collection(session, collection_id)
            if collection is None or target_user_id == collection.user_id:
                return
            await session.execute(
                insert(CollectionCollaborator)
                .values(
                    collection_id=collection_id,
                    user_id=target_user_id,
                    role=role,
                    status="active",
                    invited_by_id=invited_by,
                    created_at=_now(),
                    updated_at=_now(),
                )
                .on_conflict_do_update(
                    index_elements=[
                        CollectionCollaborator.collection_id,
                        CollectionCollaborator.user_id,
                    ],
                    set_={
                        "role": role,
                        "status": "active",
                        "invited_by_id": invited_by,
                        "updated_at": _now(),
                    },
                )
            )
            await _recompute_share_state(session, collection_id)

    async def async_remove_collaborator(self, collection_id: int, target_user_id: int) -> None:
        async with self._database.transaction() as session:
            collection = await _active_collection(session, collection_id)
            if collection is None or target_user_id == collection.user_id:
                return
            await session.execute(
                delete(CollectionCollaborator).where(
                    CollectionCollaborator.collection_id == collection_id,
                    CollectionCollaborator.user_id == target_user_id,
                )
            )
            await _recompute_share_state(session, collection_id)

    async def async_list_collaborators(self, collection_id: int) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(CollectionCollaborator)
                    .where(CollectionCollaborator.collection_id == collection_id)
                    .order_by(CollectionCollaborator.created_at.asc())
                )
            ).scalars()
            return [_collaborator_dict(row) for row in rows]

    async def async_get_owner_info(self, collection_id: int) -> dict[str, Any] | None:
        async with self._database.session() as session:
            collection = await _active_collection(session, collection_id)
            if collection is None:
                return None
            owner = await session.get(User, collection.user_id)
            owner_display: dict[str, Any] | None = None
            if owner is not None:
                # Return only non-sensitive display fields.  Excluded: preferences_json,
                # link_nonce, link_nonce_expires_at, is_owner, server_version,
                # onboarding_completed_at, linked_at — none of which are needed by any
                # ACL consumer and must not be visible to viewer-role collaborators.
                owner_display = {
                    "telegram_user_id": owner.telegram_user_id,
                    "username": owner.username,
                    "display_name": owner.display_name,
                    "locale": owner.locale,
                    "theme": owner.theme,
                    "linked_telegram_username": owner.linked_telegram_username,
                    "linked_telegram_first_name": owner.linked_telegram_first_name,
                    "linked_telegram_last_name": owner.linked_telegram_last_name,
                    "linked_telegram_photo_url": owner.linked_telegram_photo_url,
                }
            return {
                "collection_id": collection.id,
                "user_id": collection.user_id,
                "owner_user": owner_display,
                "role": "owner",
                "status": "active",
            }

    async def async_create_invite(
        self,
        collection_id: int,
        role: str,
        expires_at: dt.datetime | None,
        invited_user_id: int | None = None,
    ) -> dict[str, Any]:
        async with self._database.transaction() as session:
            if await _active_collection(session, collection_id) is None:
                return {}
            invite = CollectionInvite(
                collection_id=collection_id,
                token=uuid.uuid4().hex,
                role=role,
                expires_at=expires_at,
                invited_user_id=invited_user_id,
                status="active",
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(invite)
            await session.flush()
            return _invite_dict(invite)

    async def async_get_invite_by_token(self, token: str) -> dict[str, Any] | None:
        async with self._database.session() as session:
            invite = await session.scalar(
                select(CollectionInvite).where(CollectionInvite.token == token)
            )
            return _invite_dict(invite) if invite else None

    async def async_list_incoming_invites(
        self,
        user_id: int,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(CollectionInvite, Collection)
                    .join(Collection, CollectionInvite.collection_id == Collection.id)
                    .where(
                        CollectionInvite.invited_user_id == user_id,
                        CollectionInvite.status == "active",
                        CollectionInvite.used_at.is_(None),
                        Collection.is_deleted.is_(False),
                    )
                    .order_by(CollectionInvite.created_at.desc(), CollectionInvite.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            collection_counts = await _item_counts(
                session, [collection.id for _, collection in rows]
            )
            invites: list[dict[str, Any]] = []
            now = _now()
            for invite, collection in rows:
                invite_data = _invite_dict(invite)
                collection_data = _collection_dict(collection)
                collection_data["item_count"] = collection_counts.get(collection.id, 0)
                invite_data["collection"] = collection_data
                invite_data["invited_by"] = collection.user_id
                expires_at = coerce_datetime(invite.expires_at) if invite.expires_at else None
                invite_data["status"] = "expired" if expires_at and expires_at < now else "pending"
                invites.append(invite_data)
            return invites

    async def async_update_invite(self, invite_id: int, **fields: Any) -> None:
        allowed = set(CollectionInvite.__table__.columns.keys()) - {
            "id",
            "collection_id",
            "created_at",
        }
        update_fields = {key: value for key, value in fields.items() if key in allowed}
        if not update_fields:
            return
        update_fields["updated_at"] = _now()
        async with self._database.transaction() as session:
            await session.execute(
                update(CollectionInvite)
                .where(CollectionInvite.id == invite_id)
                .values(**update_fields)
            )

    async def async_accept_invite(
        self,
        token: str,
        user_id: int,
    ) -> dict[str, Any] | None:
        async with self._database.transaction() as session:
            invite = await session.scalar(
                select(CollectionInvite).where(CollectionInvite.token == token)
            )
            if invite is None or invite.status in {"used", "revoked"}:
                return None
            if invite.invited_user_id is not None and invite.invited_user_id != user_id:
                return None
            expires_at = coerce_datetime(invite.expires_at) if invite.expires_at else None
            if expires_at and expires_at < _now():
                invite.status = "expired"
                invite.updated_at = _now()
                return None
            collection = await _active_collection(session, invite.collection_id)
            if collection is None:
                return None
            if user_id != collection.user_id:
                await session.execute(
                    insert(CollectionCollaborator)
                    .values(
                        collection_id=collection.id,
                        user_id=user_id,
                        role=invite.role,
                        status="active",
                        invited_by_id=collection.user_id,
                        created_at=_now(),
                        updated_at=_now(),
                    )
                    .on_conflict_do_update(
                        index_elements=[
                            CollectionCollaborator.collection_id,
                            CollectionCollaborator.user_id,
                        ],
                        set_={
                            "role": invite.role,
                            "status": "active",
                            "invited_by_id": collection.user_id,
                            "updated_at": _now(),
                        },
                    )
                )
                await _recompute_share_state(session, collection.id)
            invite.used_at = _now()
            invite.status = "used"
            invite.updated_at = _now()
            return {"collection_id": collection.id, "role": invite.role, "status": "accepted"}

    async def async_create_public_link(
        self,
        *,
        collection_id: int,
        token: str,
        expires_at: dt.datetime | None,
        password_hash: str | None,
    ) -> dict[str, Any]:
        async with self._database.transaction() as session:
            if await _active_collection(session, collection_id) is None:
                return {}
            link = CollectionPublicLink(
                collection_id=collection_id,
                token=token,
                expires_at=expires_at,
                password_hash=password_hash,
                created_at=_now(),
                view_count=0,
            )
            session.add(link)
            await session.flush()
            return _public_link_dict(link)

    async def async_list_public_links(self, collection_id: int) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(CollectionPublicLink)
                    .where(
                        CollectionPublicLink.collection_id == collection_id,
                        CollectionPublicLink.revoked_at.is_(None),
                        or_(
                            CollectionPublicLink.expires_at.is_(None),
                            CollectionPublicLink.expires_at > _now(),
                        ),
                    )
                    .order_by(
                        CollectionPublicLink.created_at.desc(), CollectionPublicLink.id.desc()
                    )
                )
            ).scalars()
            return [_public_link_dict(link) for link in rows]

    async def async_revoke_public_link(self, collection_id: int, token: str) -> bool:
        async with self._database.transaction() as session:
            result = await session.execute(
                update(CollectionPublicLink)
                .where(
                    CollectionPublicLink.collection_id == collection_id,
                    CollectionPublicLink.token == token,
                    CollectionPublicLink.revoked_at.is_(None),
                )
                .values(revoked_at=_now())
                .returning(CollectionPublicLink.id)
            )
            return result.scalar_one_or_none() is not None

    async def async_get_public_link_by_token(
        self, token: str, *, include_password_hash: bool = False
    ) -> dict[str, Any] | None:
        async with self._database.session() as session:
            link = await session.scalar(
                select(CollectionPublicLink).where(CollectionPublicLink.token == token)
            )
            return (
                _public_link_dict(link, include_password_hash=include_password_hash)
                if link
                else None
            )

    async def async_get_public_collection_payload(
        self,
        token: str,
        *,
        viewer_ip: str | None,
    ) -> dict[str, Any] | None:
        async with self._database.transaction() as session:
            link = await session.scalar(
                select(CollectionPublicLink).where(CollectionPublicLink.token == token)
            )
            if link is None or link.revoked_at is not None:
                return None
            expires_at = coerce_datetime(link.expires_at) if link.expires_at else None
            if expires_at is not None and expires_at <= _now():
                return None
            collection = await _active_collection(session, link.collection_id)
            if collection is None:
                return None
            owner = await session.get(User, collection.user_id)
            rows = (
                await session.execute(
                    select(CollectionItem, Summary, Request)
                    .join(Summary, CollectionItem.summary_id == Summary.id)
                    .join(Request, Summary.request_id == Request.id)
                    .where(
                        CollectionItem.collection_id == collection.id,
                        Summary.is_deleted.is_(False),
                        Request.is_deleted.is_(False),
                    )
                    .order_by(CollectionItem.position.asc(), CollectionItem.created_at.asc())
                )
            ).all()
            link.view_count = int(link.view_count or 0) + 1
            session.add(
                AuditLog(
                    level="info",
                    event="collection_public_link_read",
                    details_json={
                        "collection_id": collection.id,
                        "public_link_id": link.id,
                        "viewer_ip": viewer_ip,
                    },
                )
            )
            await session.flush()
            return {
                "link": _public_link_dict(link),
                "collection": _collection_dict(collection),
                "owner": model_to_dict(owner) if owner else None,
                "items": [
                    _public_collection_item_dict(item, summary, request)
                    for item, summary, request in rows
                ],
            }

    async def async_list_smart_collections_for_user(self, user_id: int) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(Collection)
                    .where(
                        Collection.user_id == user_id,
                        Collection.collection_type == "smart",
                        Collection.is_deleted.is_(False),
                    )
                    .order_by(Collection.created_at.asc())
                )
            ).scalars()
            return [model_to_dict(collection) or {} for collection in rows]

    # 5C — page size for smart-collection candidate scan.  Replaces the
    # previous hard limit=10000 that silently dropped older summaries.
    _SMART_SCAN_PAGE_SIZE: int = 500

    async def async_list_user_summaries_with_request(self, user_id: int) -> list[dict[str, Any]]:
        """Return all non-deleted summaries with their request for a user.

        Previous implementation had a hard limit=10000 that silently dropped
        older summaries from the smart-collection evaluation (audit finding 5C).
        This replaces it with a keyset-paginated scan so *every* qualifying row
        is evaluated regardless of total count.

        The ``limit`` parameter has been removed.  Callers that passed a custom
        limit (e.g. for testing) should paginate themselves or pass a max_count
        argument; all production callers in CollectionService only passed the
        default, so this is safe.
        """
        results: list[dict[str, Any]] = []
        last_id = 0
        async with self._database.session() as session:
            while True:
                rows = (
                    await session.execute(
                        select(Summary, Request)
                        .join(Request, Summary.request_id == Request.id)
                        .where(
                            Request.user_id == user_id,
                            Summary.is_deleted.is_(False),
                            Summary.id > last_id,
                        )
                        .order_by(Summary.id)
                        .limit(self._SMART_SCAN_PAGE_SIZE)
                    )
                ).all()
                if not rows:
                    break
                for summary, request in rows:
                    results.append(
                        {
                            "summary": model_to_dict(summary) or {},
                            "request": model_to_dict(request) or {},
                        }
                    )
                last_id = rows[-1][0].id
                if len(rows) < self._SMART_SCAN_PAGE_SIZE:
                    break
        return results


async def _active_collection(session: Any, collection_id: int | None) -> Collection | None:
    if collection_id is None:
        return None
    return cast(
        "Collection | None",
        await session.scalar(
            select(Collection).where(
                Collection.id == collection_id, Collection.is_deleted.is_(False)
            )
        ),
    )


async def _item_count(session: Any, collection_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count(CollectionItem.id)).where(
                CollectionItem.collection_id == collection_id
            )
        )
        or 0
    )


async def _serialize_collection(
    session: Any, collection: Collection | None
) -> dict[str, Any] | None:
    if collection is None:
        return None
    data = _collection_dict(collection)
    data["item_count"] = await _item_count(session, collection.id)
    return data


async def _item_counts(session: Any, collection_ids: list[int]) -> dict[int, int]:
    """Return {collection_id: item_count} for many collections in one query."""
    if not collection_ids:
        return {}
    rows = await session.execute(
        select(CollectionItem.collection_id, func.count(CollectionItem.id))
        .where(CollectionItem.collection_id.in_(collection_ids))
        .group_by(CollectionItem.collection_id)
    )
    return {int(cid): int(count) for cid, count in rows.all()}


async def _serialize_collections(
    session: Any, collections: list[Collection]
) -> list[dict[str, Any]]:
    """Serialize collections with item counts resolved in a single grouped query.

    Avoids the per-row COUNT that _serialize_collection issues, so listing N
    collections is O(1) queries instead of 1 + N.
    """
    counts = await _item_counts(session, [collection.id for collection in collections])
    serialized: list[dict[str, Any]] = []
    for collection in collections:
        data = _collection_dict(collection)
        data["item_count"] = counts.get(collection.id, 0)
        serialized.append(data)
    return serialized


def _collection_dict(collection: Collection) -> dict[str, Any]:
    data = model_to_dict(collection) or {}
    data["parent"] = data.get("parent_id")
    data["user"] = data.get("user_id")
    return data


def _item_dict(item: CollectionItem) -> dict[str, Any]:
    data = model_to_dict(item) or {}
    data["collection"] = data.get("collection_id")
    data["summary"] = data.get("summary_id")
    return data


def _collaborator_dict(collaborator: CollectionCollaborator) -> dict[str, Any]:
    data = model_to_dict(collaborator) or {}
    data["collection"] = data.get("collection_id")
    data["user"] = data.get("user_id")
    data["invited_by"] = data.get("invited_by_id")
    return data


def _invite_dict(invite: CollectionInvite) -> dict[str, Any]:
    data = model_to_dict(invite) or {}
    data["collection"] = data.get("collection_id")
    return data


def _public_link_dict(
    link: CollectionPublicLink, *, include_password_hash: bool = False
) -> dict[str, Any]:
    data = model_to_dict(link) or {}
    data["collection"] = data.get("collection_id")
    data["has_password"] = bool(data.get("password_hash"))
    if not include_password_hash:
        data.pop("password_hash", None)
    return data


def _public_collection_item_dict(
    item: CollectionItem, summary: Summary, request: Request
) -> dict[str, Any]:
    payload = summary.json_payload if isinstance(summary.json_payload, dict) else {}
    return {
        "collection_id": item.collection_id,
        "summary_id": summary.id,
        "position": item.position,
        "title": _first_text(
            summary.title,
            payload.get("title"),
            payload.get("tldr"),
            request.normalized_url,
            request.input_url,
            default=f"Summary {summary.id}",
        ),
        "url": request.normalized_url or request.input_url,
        "summary_250": _first_text(
            payload.get("summary_250"),
            payload.get("tldr"),
            payload.get("summary_1000"),
            default="",
        ),
        "tldr": payload.get("tldr") if isinstance(payload.get("tldr"), str) else None,
        "created_at": summary.created_at,
    }


def _first_text(*values: Any, default: str) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


async def _touch_collection(session: Any, collection_id: int) -> None:
    await _touch_collections(session, [collection_id])


async def _touch_collections(session: Any, collection_ids: list[int]) -> None:
    unique_collection_ids = list(dict.fromkeys(collection_ids))
    if not unique_collection_ids:
        return
    await session.execute(
        update(Collection).where(Collection.id.in_(unique_collection_ids)).values(updated_at=_now())
    )


async def _recompute_share_state(session: Any, collection_id: int) -> None:
    share_count = int(
        await session.scalar(
            select(func.count(CollectionCollaborator.id)).where(
                CollectionCollaborator.collection_id == collection_id,
                CollectionCollaborator.status == "active",
            )
        )
        or 0
    )
    await session.execute(
        update(Collection)
        .where(Collection.id == collection_id)
        .values(share_count=share_count, is_shared=share_count > 0, updated_at=_now())
    )
