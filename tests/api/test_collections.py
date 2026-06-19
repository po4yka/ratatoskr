"""
Tests for collections management endpoints (direct calls).
"""

import pytest

from app.api.models.requests import (
    CollectionCreateRequest,
    CollectionInviteRequest,
    CollectionItemCreateRequest,
    CollectionUpdateRequest,
)
from app.api.routers import collections
from app.api.services.collection_service import CollectionService
from app.db.models import Collection, CollectionItem


@pytest.mark.asyncio
async def test_create_collection(db, user_factory):
    user = user_factory(username="col_user")
    user_context = {"user_id": user.telegram_user_id}

    body = CollectionCreateRequest(name="My Favs", description="Desc")
    response = await collections.create_collection(body=body, user=user_context)

    assert response["success"] is True
    data = response["data"]
    assert data["name"] == "My Favs"
    assert "id" in data


@pytest.mark.asyncio
async def test_get_collections(db, user_factory):
    user = user_factory(username="col_user_list")
    user_context = {"user_id": user.telegram_user_id}

    # Create via DB or router
    await collections.create_collection(CollectionCreateRequest(name="C1"), user=user_context)
    await collections.create_collection(CollectionCreateRequest(name="C2"), user=user_context)

    response = await collections.get_collections(user=user_context)
    data = response["data"]["collections"]
    assert len(data) >= 2
    names = [c["name"] for c in data]
    assert "C1" in names
    assert "C2" in names


@pytest.mark.asyncio
async def test_get_collections_membership_filters(db, user_factory):
    owner = user_factory(username="col_filter_owner", telegram_user_id=7101)
    collaborator = user_factory(username="col_filter_collab", telegram_user_id=7102)
    collaborator_context = {"user_id": collaborator.telegram_user_id}

    owned = await CollectionService.create_collection(
        user_id=collaborator.telegram_user_id,
        name="Collaborator Owned",
        description=None,
        parent_id=None,
        position=None,
    )
    shared = await CollectionService.create_collection(
        user_id=owner.telegram_user_id,
        name="Owner Shared",
        description=None,
        parent_id=None,
        position=None,
    )
    await CollectionService.add_collaborator(
        shared["id"],
        owner.telegram_user_id,
        collaborator.telegram_user_id,
        "viewer",
    )

    owned_response = await collections.get_collections(
        user=collaborator_context,
        membership="owned",
    )
    shared_response = await collections.get_collections(
        user=collaborator_context,
        membership="shared",
    )
    any_response = await collections.get_collections(
        user=collaborator_context,
        membership="any",
    )

    assert {item["id"] for item in owned_response["data"]["collections"]} == {owned["id"]}
    assert {item["id"] for item in shared_response["data"]["collections"]} == {shared["id"]}
    assert {item["id"] for item in any_response["data"]["collections"]} == {
        owned["id"],
        shared["id"],
    }


@pytest.mark.asyncio
async def test_list_incoming_collection_invites(db, user_factory):
    owner = user_factory(username="col_invite_owner", telegram_user_id=7111)
    invitee = user_factory(username="col_invite_invitee", telegram_user_id=7112)
    other = user_factory(username="col_invite_other", telegram_user_id=7113)
    collection = await CollectionService.create_collection(
        user_id=owner.telegram_user_id,
        name="Invite Target",
        description=None,
        parent_id=None,
        position=None,
    )
    await collections.create_collection_invite(
        collection_id=collection["id"],
        body=CollectionInviteRequest(role="viewer", recipient_user_id=invitee.telegram_user_id),
        user={"user_id": owner.telegram_user_id},
    )
    await collections.create_collection_invite(
        collection_id=collection["id"],
        body=CollectionInviteRequest(role="viewer", recipient_user_id=other.telegram_user_id),
        user={"user_id": owner.telegram_user_id},
    )

    response = await collections.list_incoming_collection_invites(
        user={"user_id": invitee.telegram_user_id},
    )

    invites = response["data"]["invites"]
    assert len(invites) == 1
    assert invites[0]["collection"]["id"] == collection["id"]
    assert invites[0]["status"] == "pending"
    assert invites[0]["invitedBy"] == owner.telegram_user_id


@pytest.mark.asyncio
async def test_update_collection(db, user_factory):
    user = user_factory(username="col_user_update")
    user_context = {"user_id": user.telegram_user_id}

    create_resp = await collections.create_collection(
        CollectionCreateRequest(name="Orig"), user=user_context
    )
    cid = create_resp["data"]["id"]

    response = await collections.update_collection(
        collection_id=cid,
        body=CollectionUpdateRequest(name="New", description="NewD"),
        user=user_context,
    )
    assert response["data"]["name"] == "New"
    assert response["data"]["description"] == "NewD"


@pytest.mark.asyncio
async def test_delete_collection(db, user_factory):
    user = user_factory(username="col_user_del")
    user_context = {"user_id": user.telegram_user_id}

    create_resp = await collections.create_collection(
        CollectionCreateRequest(name="ToDel"), user=user_context
    )
    cid = create_resp["data"]["id"]

    await collections.delete_collection(collection_id=cid, user=user_context)

    # Verify soft deletion
    deleted = Collection.get_or_none(Collection.id == cid)
    assert deleted is not None
    assert deleted.is_deleted is True


@pytest.mark.asyncio
async def test_add_remove_item(db, user_factory, summary_factory):
    user = user_factory(username="col_user_item")
    user_context = {"user_id": user.telegram_user_id}

    # Create collection
    create_resp = await collections.create_collection(
        CollectionCreateRequest(name="Items"), user=user_context
    )
    cid = create_resp["data"]["id"]

    # Create summary
    summary = summary_factory(user=user)

    # Add item
    await collections.add_collection_item(
        collection_id=cid,
        body=CollectionItemCreateRequest(summary_id=summary.id),
        user=user_context,
    )

    assert (
        CollectionItem.select()
        .where((CollectionItem.collection_id == cid) & (CollectionItem.summary_id == summary.id))
        .exists()
    )

    # Remove item
    await collections.remove_collection_item(
        collection_id=cid, summary_id=summary.id, user=user_context
    )

    assert (
        not CollectionItem.select()
        .where((CollectionItem.collection_id == cid) & (CollectionItem.summary_id == summary.id))
        .exists()
    )
