from __future__ import annotations

import datetime as dt
import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, func, select

from app.config.database import DatabaseConfig
from app.core.time_utils import UTC
from app.db.models import (
    Collection,
    CollectionCollaborator,
    CollectionInvite,
    CollectionItem,
    Request,
    Summary,
    User,
)
from app.db.session import Database
from app.infrastructure.persistence.repositories.collection_repository import (
    CollectionRepositoryAdapter,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture
async def database() -> AsyncGenerator[Database]:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres repository tests")

    db = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    await db.migrate()
    await _clear(db)
    try:
        yield db
    finally:
        await _clear(db)
        await db.dispose()


async def _clear(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(delete(CollectionInvite))
        await session.execute(delete(CollectionCollaborator))
        await session.execute(delete(CollectionItem))
        await session.execute(delete(Collection))
        await session.execute(delete(Summary))
        await session.execute(delete(Request))
        await session.execute(delete(User))


async def _create_user(database: Database, *, telegram_user_id: int, username: str) -> User:
    async with database.transaction() as session:
        user = User(telegram_user_id=telegram_user_id, username=username)
        session.add(user)
        await session.flush()
        return user


async def _create_summary(database: Database, *, user: User, suffix: str) -> Summary:
    url = f"https://example.com/{suffix}"
    async with database.transaction() as session:
        request = Request(
            user_id=user.telegram_user_id,
            input_url=url,
            normalized_url=url,
            dedupe_hash=f"hash-{suffix}",
            status="completed",
            type="url",
        )
        session.add(request)
        await session.flush()
        summary = Summary(
            request_id=request.id,
            lang="en",
            json_payload={"summary_250": f"summary-{suffix}"},
        )
        session.add(summary)
        await session.flush()
        return summary


@pytest.mark.asyncio
async def test_collection_repository_crud_tree_and_move_operations(database: Database) -> None:
    repo = CollectionRepositoryAdapter(database)
    owner = await _create_user(database, telegram_user_id=7001, username="owner-collections")

    root_id = await repo.async_create_collection(
        user_id=owner.telegram_user_id,
        name="Root",
        description=None,
        parent_id=None,
        position=1,
    )
    second_root_id = await repo.async_create_collection(
        user_id=owner.telegram_user_id,
        name="Second Root",
        description="second",
        parent_id=None,
        position=2,
    )
    child_id = await repo.async_create_collection(
        user_id=owner.telegram_user_id,
        name="Child",
        description="nested",
        parent_id=root_id,
        position=1,
    )

    listed = await repo.async_list_collections(owner.telegram_user_id, None, limit=10, offset=0)
    assert [item["name"] for item in listed] == ["Root", "Second Root"]
    assert await repo.async_get_next_position(None) == 3

    tree = await repo.async_get_collection_tree(owner.telegram_user_id)
    ids = {item["id"] for item in tree}
    assert {root_id, second_root_id, child_id}.issubset(ids)

    await repo.async_update_collection(
        child_id, owner.telegram_user_id, name="Renamed Child", description="updated"
    )
    moved = await repo.async_move_collection(child_id, None, 1)
    assert moved is not None
    assert moved["parent"] is None
    assert moved["position"] == 1

    await repo.async_reorder_collections(
        None,
        [
            {"collection_id": child_id, "position": 1},
            {"collection_id": root_id, "position": 2},
            {"collection_id": second_root_id, "position": 3},
        ],
    )
    renamed = await repo.async_get_collection(child_id)
    assert renamed is not None
    assert renamed["name"] == "Renamed Child"
    assert renamed["item_count"] == 0

    await repo.async_soft_delete_collection(second_root_id, owner.telegram_user_id)
    assert await repo.async_get_collection(second_root_id) is None
    deleted = await repo.async_get_collection(second_root_id, include_deleted=True)
    assert deleted is not None
    assert deleted["is_deleted"] is True


@pytest.mark.asyncio
async def test_collection_repository_item_and_smart_collection_operations(
    database: Database,
) -> None:
    repo = CollectionRepositoryAdapter(database)
    owner = await _create_user(database, telegram_user_id=7002, username="owner-items")
    source_id = await repo.async_create_collection(
        user_id=owner.telegram_user_id,
        name="Source",
        description=None,
        parent_id=None,
        position=1,
    )
    target_id = await repo.async_create_collection(
        user_id=owner.telegram_user_id,
        name="Target",
        description=None,
        parent_id=None,
        position=2,
    )
    smart_id = await repo.async_create_collection(
        user_id=owner.telegram_user_id,
        name="Smart",
        description=None,
        parent_id=None,
        position=3,
        collection_type="smart",
        query_conditions_json=[{"field": "topic", "op": "contains", "value": "ai"}],
        query_match_mode="all",
    )
    summary_a = await _create_summary(database, user=owner, suffix="a")
    summary_b = await _create_summary(database, user=owner, suffix="b")

    assert await repo.async_add_item(source_id, summary_a.id, 1) is True
    assert await repo.async_add_item(source_id, summary_a.id, 1) is False
    assert await repo.async_add_item(source_id, summary_b.id, 2) is True
    assert await repo.async_get_item_count(source_id) == 2
    assert await repo.async_get_next_item_position(source_id) == 3

    await repo.async_reorder_items(
        source_id,
        [
            {"summary_id": summary_b.id, "position": 1},
            {"summary_id": summary_a.id, "position": 2},
        ],
    )
    moved = await repo.async_move_items(source_id, target_id, [summary_a.id], 1)
    assert moved == [summary_a.id]

    missing_summary_id = max(summary_a.id, summary_b.id) + 100_000
    inserted = await repo.async_bulk_set_items(
        target_id,
        [missing_summary_id, summary_b.id, summary_b.id, summary_a.id],
    )
    assert inserted == 2
    target_positions = {
        item["summary"]: item["position"]
        for item in await repo.async_list_items(target_id, limit=10, offset=0)
    }
    assert target_positions == {summary_b.id: 2, summary_a.id: 4}

    await repo.async_shift_item_positions(target_id, 1)
    target_items = await repo.async_list_items(target_id, limit=10, offset=0)
    assert len(target_items) == 2
    assert all(item["position"] >= 2 for item in target_items)

    await repo.async_remove_item(target_id, summary_b.id)
    assert await repo.async_get_item_count(target_id) == 1

    smart_collections = await repo.async_list_smart_collections_for_user(owner.telegram_user_id)
    assert [item["id"] for item in smart_collections] == [smart_id]

    rows = await repo.async_list_user_summaries_with_request(owner.telegram_user_id)
    request_ids = {row["request"]["id"] for row in rows}
    assert len(rows) == 2
    assert request_ids == {summary_a.request_id, summary_b.request_id}


@pytest.mark.asyncio
async def test_collection_repository_move_items_preserves_target_conflict_shift(
    database: Database,
) -> None:
    repo = CollectionRepositoryAdapter(database)
    owner = await _create_user(database, telegram_user_id=7012, username="owner-move-conflict")
    source_id = await repo.async_create_collection(
        user_id=owner.telegram_user_id,
        name="Conflict Source",
        description=None,
        parent_id=None,
        position=1,
    )
    target_id = await repo.async_create_collection(
        user_id=owner.telegram_user_id,
        name="Conflict Target",
        description=None,
        parent_id=None,
        position=2,
    )
    summary_a = await _create_summary(database, user=owner, suffix="conflict-a")
    summary_b = await _create_summary(database, user=owner, suffix="conflict-b")

    assert await repo.async_add_item(source_id, summary_a.id, 1) is True
    assert await repo.async_add_item(source_id, summary_b.id, 2) is True
    assert await repo.async_add_item(target_id, summary_a.id, 1) is True

    moved = await repo.async_move_items(source_id, target_id, [summary_a.id, summary_b.id], 1)

    assert moved == [summary_b.id]
    assert await repo.async_list_items(source_id, limit=10, offset=0) == []
    target_items = await repo.async_list_items(target_id, limit=10, offset=0)
    assert [(item["summary"], item["position"]) for item in target_items] == [
        (summary_b.id, 1),
        (summary_a.id, 3),
    ]


@pytest.mark.asyncio
async def test_collection_repository_acl_and_invite_flow(database: Database) -> None:
    repo = CollectionRepositoryAdapter(database)
    owner = await _create_user(database, telegram_user_id=7003, username="owner-acl")
    collaborator = await _create_user(database, telegram_user_id=7004, username="collaborator-acl")
    invitee = await _create_user(database, telegram_user_id=7005, username="invitee-acl")
    collection_id = await repo.async_create_collection(
        user_id=owner.telegram_user_id,
        name="Shared",
        description=None,
        parent_id=None,
        position=1,
    )

    assert await repo.async_get_role(collection_id, owner.telegram_user_id) == "owner"
    assert await repo.async_get_role(collection_id, collaborator.telegram_user_id) is None

    await repo.async_add_collaborator(
        collection_id,
        collaborator.telegram_user_id,
        "editor",
        invited_by=owner.telegram_user_id,
    )
    assert await repo.async_get_role(collection_id, collaborator.telegram_user_id) == "editor"

    collaborators = await repo.async_list_collaborators(collection_id)
    assert any(entry["user"] == collaborator.telegram_user_id for entry in collaborators)
    async with database.session() as session:
        exists = await session.scalar(
            select(CollectionCollaborator.id).where(
                CollectionCollaborator.collection_id == collection_id,
                CollectionCollaborator.user_id == collaborator.telegram_user_id,
            )
        )
    assert exists is not None

    owner_info = await repo.async_get_owner_info(collection_id)
    assert owner_info is not None
    assert owner_info["owner_user"]["telegram_user_id"] == owner.telegram_user_id

    await repo.async_remove_collaborator(collection_id, collaborator.telegram_user_id)
    assert await repo.async_get_role(collection_id, collaborator.telegram_user_id) is None

    invite = await repo.async_create_invite(
        collection_id,
        "viewer",
        dt.datetime.now(UTC) + dt.timedelta(days=1),
    )
    fetched = await repo.async_get_invite_by_token(invite["token"])
    assert fetched is not None
    await repo.async_update_invite(fetched["id"], role="editor")

    accepted = await repo.async_accept_invite(invite["token"], invitee.telegram_user_id)
    assert accepted == {
        "collection_id": collection_id,
        "role": "editor",
        "status": "accepted",
    }
    assert await repo.async_get_role(collection_id, invitee.telegram_user_id) == "editor"
    assert await repo.async_accept_invite(invite["token"], collaborator.telegram_user_id) is None
    async with database.session() as session:
        item_count = await session.scalar(
            select(func.count(CollectionItem.id)).where(
                CollectionItem.collection_id == collection_id
            )
        )
    assert item_count == 0
