"""
Collections management endpoints.
"""

from datetime import datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Header, Query, Request

from app.api.exceptions import ValidationError
from app.api.models.requests import (
    CollectionCreateRequest,
    CollectionInviteRequest,
    CollectionItemCreateRequest,
    CollectionItemMoveRequest,
    CollectionItemReorderRequest,
    CollectionMoveRequest,
    CollectionPublicLinkCreateRequest,
    CollectionReorderRequest,
    CollectionShareRequest,
    CollectionUpdateRequest,
)
from app.api.models.responses import (
    CollectionAclEntry,
    CollectionAclResponse,
    CollectionIncomingInvite,
    CollectionIncomingInvitesResponse,
    CollectionItem,
    CollectionItemsMoveResponse,
    CollectionItemsResponse,
    CollectionListResponse,
    CollectionMoveResponse,
    CollectionPublicLinkListResponse,
    CollectionPublicLinkListSuccessResponse,
    CollectionPublicLinkRevocationResponse,
    CollectionPublicLinkRevocationSuccessResponse,
    CollectionPublicLinkResponse,
    CollectionPublicLinkSuccessResponse,
    CollectionResponse,
    PaginationInfo,
    PublicCollectionItemResponse,
    PublicCollectionResponse,
    PublicCollectionSuccessResponse,
    success_response,
)
from app.api.routers.auth import get_current_user
from app.api.search_helpers import isotime
from app.api.services.collection_service import CollectionService
from app.core.logging_utils import get_logger

logger = get_logger(__name__)
router = APIRouter()
public_router = APIRouter()
CollectionMembership = Literal["any", "owned", "shared"]


def get_collection_service(request: Request) -> CollectionService:
    from app.di.api import resolve_api_runtime

    return cast("CollectionService", resolve_api_runtime(request).collection_service)


def _build_collection_response(c: dict[str, Any]) -> CollectionResponse:
    """Build a CollectionResponse from a collection dict."""
    return CollectionResponse(
        id=c["id"],
        name=c["name"],
        description=c.get("description"),
        parent_id=c.get("parent_id") or c.get("parent"),
        position=c.get("position"),
        created_at=isotime(c.get("created_at")),
        updated_at=isotime(c.get("updated_at")),
        server_version=c.get("server_version"),
        is_shared=bool(c.get("is_shared", False)),
        share_count=c.get("share_count"),
        item_count=c.get("item_count", 0),
        collection_type=c.get("collection_type", "manual"),
        query_conditions=c.get("query_conditions_json"),
        query_match_mode=c.get("query_match_mode"),
        last_evaluated_at=isotime(c.get("last_evaluated_at"))
        if c.get("last_evaluated_at")
        else None,
    )


def _build_incoming_invite_response(invite: dict[str, Any]) -> CollectionIncomingInvite:
    """Build a CollectionIncomingInvite from a repository invite dict."""
    return CollectionIncomingInvite(
        id=invite["id"],
        token=invite["token"],
        role=invite.get("role", "viewer"),
        status=invite.get("status", "pending"),
        collection=_build_collection_response(invite["collection"]),
        invited_by=invite["invited_by"],
        created_at=isotime(invite.get("created_at")),
        expires_at=isotime(invite.get("expires_at")) if invite.get("expires_at") else None,
    )


def _build_public_link_response(
    link: dict[str, Any], public_url: str
) -> CollectionPublicLinkResponse:
    return CollectionPublicLinkResponse(
        token=link["token"],
        url=public_url,
        collection_id=link.get("collection_id") or link.get("collection"),
        created_at=isotime(link.get("created_at")),
        expires_at=isotime(link.get("expires_at")) if link.get("expires_at") else None,
        revoked_at=isotime(link.get("revoked_at")) if link.get("revoked_at") else None,
        has_password=bool(link.get("has_password")),
        view_count=int(link.get("view_count") or 0),
    )


def _build_public_collection_response(payload: dict[str, Any]) -> PublicCollectionResponse:
    collection = payload["collection"]
    link = payload["link"]
    owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else {}
    return PublicCollectionResponse(
        collection_id=collection["id"],
        name=collection["name"],
        description=collection.get("description"),
        owner_display_name=owner.get("display_name") or owner.get("username"),
        view_count=int(link.get("view_count") or 0),
        items=[
            PublicCollectionItemResponse(
                summary_id=item["summary_id"],
                title=item["title"],
                url=item.get("url"),
                summary_250=item.get("summary_250") or "",
                tldr=item.get("tldr"),
                created_at=isotime(item.get("created_at")),
            )
            for item in payload.get("items", [])
        ],
    )


@router.get("")
async def get_collections(
    parent_id: int | None = Query(default=None, ge=1),
    membership: CollectionMembership = Query(default="any"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    """List collections for the current user (and collaborations) under a parent."""
    if not isinstance(parent_id, (int, type(None))):
        parent_id = None
    if not isinstance(limit, int):
        limit = 20
    if not isinstance(offset, int):
        offset = 0
    collections = await service.list_collections(
        user_id=user["user_id"],
        parent_id=parent_id,
        limit=limit,
        offset=offset,
        membership=membership,
    )
    data = [_build_collection_response(c) for c in collections]
    pagination = PaginationInfo(
        total=len(data),
        limit=limit,
        offset=offset,
        has_more=len(data) == limit,
    )
    return success_response(
        CollectionListResponse(collections=data, pagination=pagination),
        pagination=pagination,
    )


@router.get("/invites/incoming")
async def list_incoming_collection_invites(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    """List pending collection invites addressed to the current user."""
    invites = await service.list_incoming_invites(
        user_id=user["user_id"],
        limit=limit,
        offset=offset,
    )
    data = [_build_incoming_invite_response(invite) for invite in invites]
    pagination = PaginationInfo(
        total=len(data),
        limit=limit,
        offset=offset,
        has_more=len(data) == limit,
    )
    return success_response(
        CollectionIncomingInvitesResponse(invites=data, pagination=pagination),
        pagination=pagination,
    )


@router.post("")
async def create_collection(
    body: CollectionCreateRequest,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    """Create a new collection."""
    try:
        collection = await service.create_collection(
            user_id=user["user_id"],
            name=body.name,
            description=body.description,
            parent_id=body.parent_id,
            position=body.position,
            collection_type=body.collection_type,
            query_conditions=body.query_conditions,
            query_match_mode=body.query_match_mode,
        )
    except ValueError as err:
        raise ValidationError(str(err)) from err

    return success_response(_build_collection_response(collection))


@router.get("/tree")
async def get_collection_tree(
    max_depth: int = Query(3, ge=1, le=10),
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    tree = await service.get_tree(user_id=user["user_id"], max_depth=max_depth)

    def to_response(col: dict[str, Any]) -> CollectionResponse:
        children = col.get("_children") or []
        resp = _build_collection_response(col)
        resp.children = [to_response(c) for c in children]
        return resp

    data = [to_response(c) for c in tree]
    return success_response({"collections": data})


@router.get("/{collection_id}")
async def get_collection(
    collection_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    """Get collection details."""
    collection = await service.get_collection_with_auth(collection_id, user["user_id"], "viewer")

    return success_response(_build_collection_response(collection))


@router.patch("/{collection_id}")
async def update_collection(
    collection_id: int,
    body: CollectionUpdateRequest,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    """Update a collection."""
    try:
        collection = await service.update_collection(
            collection_id=collection_id,
            user_id=user["user_id"],
            name=body.name,
            description=body.description,
            parent_id=body.parent_id,
            position=body.position,
            query_conditions=body.query_conditions,
            query_match_mode=body.query_match_mode,
        )
    except ValueError as err:
        raise ValidationError(str(err)) from err

    return success_response(_build_collection_response(collection))


@router.delete("/{collection_id}")
async def delete_collection(
    collection_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    """Delete a collection (soft delete)."""
    await service.delete_collection(collection_id, user["user_id"])
    return success_response({"success": True})


@router.post("/{collection_id}/items")
async def add_collection_item(
    collection_id: int,
    body: CollectionItemCreateRequest,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    """Add a summary to a collection."""
    await service.add_item(collection_id, body.summary_id, user["user_id"])
    return success_response({"success": True})


@router.get("/{collection_id}/items")
async def list_collection_items(
    collection_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    items = await service.list_items(collection_id, user["user_id"], limit, offset)
    payload = [
        CollectionItem(
            collection_id=item.get("collection_id") or item.get("collection"),
            summary_id=item.get("summary_id") or item.get("summary"),
            position=item.get("position"),
            created_at=isotime(item.get("created_at")),
        )
        for item in items
    ]
    pagination = PaginationInfo(
        total=len(payload),
        limit=limit,
        offset=offset,
        has_more=len(payload) == limit,
    )
    return success_response(
        CollectionItemsResponse(items=payload, pagination=pagination), pagination=pagination
    )


@router.post("/{collection_id}/items/reorder")
async def reorder_collection_items(
    collection_id: int,
    body: CollectionItemReorderRequest,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    await service.reorder_items(
        collection_id,
        user["user_id"],
        [item.model_dump() for item in body.items],
    )
    return success_response({"success": True})


@router.post("/{collection_id}/items/move")
async def move_collection_items(
    collection_id: int,
    body: CollectionItemMoveRequest,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    moved = await service.move_items(
        source_collection_id=collection_id,
        user_id=user["user_id"],
        summary_ids=body.summary_ids,
        target_collection_id=body.target_collection_id,
        position=body.position,
    )
    return success_response(CollectionItemsMoveResponse(moved_summary_ids=moved))


@router.delete("/{collection_id}/items/{summary_id}")
async def remove_collection_item(
    collection_id: int,
    summary_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    """Remove a summary from a collection."""
    await service.remove_item(collection_id, summary_id, user["user_id"])
    return success_response({"success": True})


@router.post("/{collection_id}/reorder")
async def reorder_collections(
    collection_id: int,
    body: CollectionReorderRequest,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    await service.reorder_collections(
        parent_id=collection_id,
        user_id=user["user_id"],
        items=[item.model_dump() for item in body.items],
    )
    return success_response({"success": True})


@router.post("/{collection_id}/move")
async def move_collection(
    collection_id: int,
    body: CollectionMoveRequest,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    try:
        moved = await service.move_collection(
            collection_id=collection_id,
            user_id=user["user_id"],
            parent_id=body.parent_id,
            position=body.position,
        )
    except ValueError as err:
        raise ValidationError(str(err)) from err
    return success_response(
        CollectionMoveResponse(
            id=moved["id"],
            parent_id=moved.get("parent_id") or moved.get("parent"),
            position=moved.get("position") or 0,
            server_version=moved.get("server_version"),
            updated_at=isotime(moved.get("updated_at")),
        )
    )


@router.get("/{collection_id}/acl")
async def get_collection_acl(
    collection_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    acl = await service.list_acl(collection_id, user["user_id"])
    payload = []
    for entry in acl:
        # Get user_id from nested owner_user dict or direct user_id field
        entry_user_id = entry.get("user_id")
        if entry.get("owner_user"):
            entry_user_id = entry["owner_user"].get("telegram_user_id", entry_user_id)
        elif entry.get("user"):
            user_data = entry["user"]
            if isinstance(user_data, dict):
                entry_user_id = user_data.get("telegram_user_id", entry_user_id)

        # Get invited_by user id
        invited_by_id = None
        if entry.get("invited_by"):
            invited_by_data = entry["invited_by"]
            if isinstance(invited_by_data, dict):
                invited_by_id = invited_by_data.get("telegram_user_id")
            elif isinstance(invited_by_data, int):
                invited_by_id = invited_by_data

        payload.append(
            CollectionAclEntry(
                user_id=entry_user_id,
                role=entry.get("role", "owner"),
                status=entry.get("status", "active"),
                invited_by=invited_by_id,
                created_at=isotime(entry.get("created_at")) if entry.get("created_at") else None,
                updated_at=isotime(entry.get("updated_at")) if entry.get("updated_at") else None,
            )
        )
    return success_response(CollectionAclResponse(acl=payload))


@router.post("/{collection_id}/share")
async def add_collection_collaborator(
    collection_id: int,
    body: CollectionShareRequest,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    await service.add_collaborator(
        collection_id=collection_id,
        user_id=user["user_id"],
        target_user_id=body.user_id,
        role=body.role,
    )
    return success_response({"success": True})


@router.delete("/{collection_id}/share/{target_user_id}")
async def remove_collection_collaborator(
    collection_id: int,
    target_user_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    await service.remove_collaborator(
        collection_id=collection_id, user_id=user["user_id"], target_user_id=target_user_id
    )
    return success_response({"success": True})


@router.post(
    "/{collection_id}/public-link",
    response_model=CollectionPublicLinkSuccessResponse,
)
async def create_collection_public_link(
    collection_id: int,
    body: CollectionPublicLinkCreateRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    expires = None
    if body.expires_at:
        try:
            expires = datetime.fromisoformat(body.expires_at.replace("Z", "+00:00"))
        except ValueError:
            raise ValidationError("Invalid expires_at") from None
    link = await service.create_public_link(
        collection_id=collection_id,
        user_id=user["user_id"],
        expires_at=expires,
        password=body.password,
    )
    public_url = str(request.url_for("get_public_collection_by_token", token=link["token"]))
    return success_response(_build_public_link_response(link, public_url))


@router.get(
    "/{collection_id}/public-link",
    response_model=CollectionPublicLinkListSuccessResponse,
)
async def list_collection_public_links(
    collection_id: int,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    links = await service.list_public_links(collection_id, user["user_id"])
    payload = [
        _build_public_link_response(
            link,
            str(request.url_for("get_public_collection_by_token", token=link["token"])),
        )
        for link in links
    ]
    return success_response(CollectionPublicLinkListResponse(links=payload))


@router.delete(
    "/{collection_id}/public-link/{token}",
    response_model=CollectionPublicLinkRevocationSuccessResponse,
)
async def revoke_collection_public_link(
    collection_id: int,
    token: str,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    await service.revoke_public_link(
        collection_id=collection_id, token=token, user_id=user["user_id"]
    )
    return success_response(CollectionPublicLinkRevocationResponse(revoked=True))


@router.post("/{collection_id}/invite")
async def create_collection_invite(
    collection_id: int,
    body: CollectionInviteRequest,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    expires = None
    if body.expires_at:
        try:
            expires = datetime.fromisoformat(body.expires_at.replace("Z", "+00:00"))
        except ValueError:
            raise ValidationError("Invalid expires_at") from None
    invite = await service.create_invite(
        collection_id=collection_id,
        user_id=user["user_id"],
        role=body.role,
        expires_at=expires,
        recipient_user_id=body.recipient_user_id,
    )
    return success_response(
        {"token": invite.get("token"), "role": invite.get("role"), "expires_at": body.expires_at}
    )


@router.post("/invites/{token}/accept")
async def accept_collection_invite(
    token: str,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    await service.accept_invite(token=token, user_id=user["user_id"])
    return success_response({"success": True})


@router.post("/{collection_id}/evaluate")
async def evaluate_smart_collection(
    collection_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    """Force re-evaluation of a smart collection's items."""
    try:
        count = await service.evaluate_smart_collection(
            collection_id=collection_id, user_id=user["user_id"]
        )
    except ValueError as err:
        raise ValidationError(str(err)) from err
    return success_response({"item_count": count})


@public_router.get(
    "/{token}",
    name="get_public_collection_by_token",
    response_model=PublicCollectionSuccessResponse,
    openapi_extra={"security": []},
    responses={404: {"description": "Unknown, expired, revoked, or password-protected link."}},
)
async def get_public_collection_by_token(
    token: str,
    request: Request,
    x_collection_password: str | None = Header(default=None, min_length=1, max_length=256),
    service: CollectionService = Depends(get_collection_service),
) -> Any:
    viewer_ip = request.client.host if request.client else None
    payload = await service.get_public_collection(
        token=token,
        password=x_collection_password,
        viewer_ip=viewer_ip,
    )
    return success_response(_build_public_collection_response(payload))
