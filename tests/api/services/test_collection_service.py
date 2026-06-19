from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.api.exceptions import ResourceNotFoundError
from app.db.models import Collection, CollectionCollaborator, CollectionItem


@pytest.mark.asyncio
async def test_collection_service_creates_lists_builds_tree_and_reorders(
    db, user_factory, collection_service
) -> None:
    owner = user_factory(username="collection-owner", telegram_user_id=6001)

    root = await collection_service.create_collection(
        user_id=owner.telegram_user_id,
        name="Root",
        description=None,
        parent_id=None,
        position=None,
    )
    child = await collection_service.create_collection(
        user_id=owner.telegram_user_id,
        name="Child",
        description="Nested child",
        parent_id=root["id"],
        position=None,
    )
    second_root = await collection_service.create_collection(
        user_id=owner.telegram_user_id,
        name="Second Root",
        description=None,
        parent_id=None,
        position=None,
    )

    listed = await collection_service.list_collections(
        owner.telegram_user_id, None, limit=10, offset=0
    )
    assert {item["name"] for item in listed} == {"Root", "Second Root"}

    tree = await collection_service.get_tree(owner.telegram_user_id)
    root_tree = next(item for item in tree if item["name"] == "Root")
    assert root_tree["_children"][0]["id"] == child["id"]

    authorized = await collection_service.get_collection_with_auth(
        root["id"],
        owner.telegram_user_id,
        "viewer",
    )
    assert authorized["id"] == root["id"]

    with patch(
        "app.infrastructure.persistence.repositories.collection_repository.CollectionRepositoryAdapter.async_reorder_collections",
        new=AsyncMock(),
    ) as reorder:
        await collection_service.reorder_collections(
            None,
            owner.telegram_user_id,
            [
                {"collection_id": second_root["id"], "position": 1},
                {"collection_id": root["id"], "position": 2},
            ],
        )

    reorder.assert_awaited_once_with(
        None,
        [
            {"collection_id": second_root["id"], "position": 1},
            {"collection_id": root["id"], "position": 2},
        ],
    )


@pytest.mark.asyncio
async def test_collection_service_updates_moves_and_soft_deletes(
    db, user_factory, collection_service
) -> None:
    owner = user_factory(username="collection-editor", telegram_user_id=6002)
    parent_a = await collection_service.create_collection(
        user_id=owner.telegram_user_id,
        name="Parent A",
        description=None,
        parent_id=None,
        position=None,
    )
    parent_b = await collection_service.create_collection(
        user_id=owner.telegram_user_id,
        name="Parent B",
        description=None,
        parent_id=None,
        position=None,
    )
    child = await collection_service.create_collection(
        user_id=owner.telegram_user_id,
        name="Child To Move",
        description=None,
        parent_id=parent_a["id"],
        position=None,
    )

    updated = await collection_service.update_collection(
        collection_id=child["id"],
        user_id=owner.telegram_user_id,
        name="Renamed Child",
        description="Updated description",
        parent_id=parent_b["id"],
    )

    assert updated["name"] == "Renamed Child"
    assert updated["description"] == "Updated description"

    with pytest.raises(ValueError, match="own parent"):
        await collection_service.update_collection(
            collection_id=child["id"],
            user_id=owner.telegram_user_id,
            name=None,
            description=None,
            parent_id=child["id"],
        )

    moved = await collection_service.move_collection(
        child["id"],
        owner.telegram_user_id,
        parent_id=None,
        position=1,
    )
    assert moved["parent"] is None
    assert moved["position"] == 1

    await collection_service.delete_collection(child["id"], owner.telegram_user_id)
    assert Collection.get_by_id(child["id"]).is_deleted is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_collection_service_item_operations_cover_add_list_reorder_move_and_remove(
    db,
    user_factory,
    summary_factory,
    collection_service,
) -> None:
    owner = user_factory(username="collection-items", telegram_user_id=6003)
    source = await collection_service.create_collection(
        user_id=owner.telegram_user_id,
        name="Source Collection",
        description=None,
        parent_id=None,
        position=None,
    )
    target = await collection_service.create_collection(
        user_id=owner.telegram_user_id,
        name="Target Collection",
        description=None,
        parent_id=None,
        position=None,
    )
    summary_a = summary_factory(user=owner)
    summary_b = summary_factory(user=owner)

    await collection_service.add_item(source["id"], summary_a.id, owner.telegram_user_id)
    await collection_service.add_item(source["id"], summary_b.id, owner.telegram_user_id)

    with pytest.raises(ResourceNotFoundError):
        await collection_service.add_item(source["id"], 999999, owner.telegram_user_id)

    items = await collection_service.list_items(
        source["id"], owner.telegram_user_id, limit=10, offset=0
    )
    assert len(items) == 2

    with patch(
        "app.infrastructure.persistence.repositories.collection_repository.CollectionRepositoryAdapter.async_reorder_items",
        new=AsyncMock(),
    ) as reorder:
        await collection_service.reorder_items(
            source["id"],
            owner.telegram_user_id,
            [
                {"summary_id": summary_b.id, "position": 1},
                {"summary_id": summary_a.id, "position": 2},
            ],
        )

    reorder.assert_awaited_once_with(
        source["id"],
        [
            {"summary_id": summary_b.id, "position": 1},
            {"summary_id": summary_a.id, "position": 2},
        ],
    )

    moved = await collection_service.move_items(
        source["id"],
        owner.telegram_user_id,
        [summary_a.id],
        target["id"],
        position=1,
    )
    assert moved == [summary_a.id]
    assert (
        CollectionItem.select()  # type: ignore[attr-defined]
        .where(
            (CollectionItem.collection_id == target["id"])
            & (CollectionItem.summary_id == summary_a.id)
        )
        .exists()
    )

    await collection_service.remove_item(target["id"], summary_a.id, owner.telegram_user_id)
    assert (
        not CollectionItem.select()  # type: ignore[attr-defined]
        .where(
            (CollectionItem.collection_id == target["id"])
            & (CollectionItem.summary_id == summary_a.id)
        )
        .exists()
    )


@pytest.mark.asyncio
async def test_collection_service_rejects_cross_user_summary_ids(
    db,
    user_factory,
    summary_factory,
    collection_service,
) -> None:
    owner = user_factory(username="collection-owner-summary", telegram_user_id=6101)
    other = user_factory(username="collection-other-summary", telegram_user_id=6102)
    collection = await collection_service.create_collection(
        user_id=owner.telegram_user_id,
        name="Owned Collection",
        description=None,
        parent_id=None,
        position=None,
    )
    other_summary = summary_factory(user=other)

    with pytest.raises(ResourceNotFoundError):
        await collection_service.add_item(
            collection["id"],
            other_summary.id,
            owner.telegram_user_id,
        )

    assert (
        not CollectionItem.select()  # type: ignore[attr-defined]
        .where(
            (CollectionItem.collection_id == collection["id"])
            & (CollectionItem.summary_id == other_summary.id)
        )
        .exists()
    )


@pytest.mark.asyncio
async def test_collection_service_move_items_skips_ids_not_in_source_collection(
    db,
    user_factory,
    summary_factory,
    collection_service,
) -> None:
    owner = user_factory(username="collection-move-absent", telegram_user_id=6103)
    source = await collection_service.create_collection(
        user_id=owner.telegram_user_id,
        name="Move Source",
        description=None,
        parent_id=None,
        position=None,
    )
    target = await collection_service.create_collection(
        user_id=owner.telegram_user_id,
        name="Move Target",
        description=None,
        parent_id=None,
        position=None,
    )
    absent_summary = summary_factory(user=owner)

    moved = await collection_service.move_items(
        source["id"],
        owner.telegram_user_id,
        [absent_summary.id],
        target["id"],
        position=1,
    )

    assert moved == []
    assert (
        not CollectionItem.select()  # type: ignore[attr-defined]
        .where(
            (CollectionItem.collection_id == target["id"])
            & (CollectionItem.summary_id == absent_summary.id)
        )
        .exists()
    )


@pytest.mark.asyncio
async def test_collection_service_collaborators_acl_and_invites(
    db, user_factory, collection_service
) -> None:
    owner = user_factory(username="collection-owner-acl", telegram_user_id=6004)
    collaborator = user_factory(username="collection-editor-acl", telegram_user_id=6005)
    invitee = user_factory(username="collection-invitee-acl", telegram_user_id=6006)
    collection = await collection_service.create_collection(
        user_id=owner.telegram_user_id,
        name="Shared Collection",
        description=None,
        parent_id=None,
        position=None,
    )

    await collection_service.add_collaborator(
        collection["id"],
        owner.telegram_user_id,
        collaborator.telegram_user_id,
        "editor",
    )

    acl = await collection_service.list_acl(collection["id"], owner.telegram_user_id)
    roles = {entry["role"] for entry in acl}
    assert roles == {"owner", "editor"}
    assert (
        CollectionCollaborator.select()  # type: ignore[attr-defined]
        .where(
            (CollectionCollaborator.collection_id == collection["id"])
            & (CollectionCollaborator.user_id == collaborator.telegram_user_id)
        )
        .exists()
    )

    await collection_service.remove_collaborator(
        collection["id"],
        owner.telegram_user_id,
        collaborator.telegram_user_id,
    )
    assert (
        not CollectionCollaborator.select()  # type: ignore[attr-defined]
        .where(
            (CollectionCollaborator.collection_id == collection["id"])
            & (CollectionCollaborator.user_id == collaborator.telegram_user_id)
        )
        .exists()
    )

    invite = await collection_service.create_invite(
        collection["id"],
        owner.telegram_user_id,
        "viewer",
        expires_at=None,
        recipient_user_id=invitee.telegram_user_id,
    )
    incoming = await collection_service.list_incoming_invites(
        invitee.telegram_user_id,
        limit=10,
        offset=0,
    )
    assert [item["token"] for item in incoming] == [invite["token"]]

    with pytest.raises(ResourceNotFoundError):
        await collection_service.accept_invite(invite["token"], collaborator.telegram_user_id)

    await collection_service.accept_invite(invite["token"], invitee.telegram_user_id)

    invited_access = await collection_service.get_collection_with_auth(
        collection["id"],
        invitee.telegram_user_id,
        "viewer",
    )
    assert invited_access["id"] == collection["id"]

    with pytest.raises(ResourceNotFoundError):
        await collection_service.accept_invite("missing-token", invitee.telegram_user_id)
