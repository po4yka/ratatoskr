# ruff: noqa: TC001
"""Collection API response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .common import PaginationInfo


class CollectionResponse(BaseModel):
    id: int
    name: str
    description: str | None = None
    parent_id: int | None = Field(default=None, serialization_alias="parentId")
    position: int | None = None
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")
    server_version: int = Field(serialization_alias="serverVersion")
    is_shared: bool = Field(default=False, serialization_alias="isShared")
    share_count: int | None = Field(default=None, serialization_alias="shareCount")
    item_count: int | None = Field(default=None, serialization_alias="itemCount")
    children: list[CollectionResponse] | None = None
    collection_type: str = Field(default="manual", serialization_alias="collectionType")
    query_conditions: list[dict[str, Any]] | None = Field(
        default=None, serialization_alias="queryConditions"
    )
    query_match_mode: str | None = Field(default=None, serialization_alias="queryMatchMode")
    last_evaluated_at: str | None = Field(default=None, serialization_alias="lastEvaluatedAt")


class CollectionListResponse(BaseModel):
    collections: list[CollectionResponse]
    pagination: PaginationInfo | None = None


class CollectionItem(BaseModel):
    collection_id: int = Field(serialization_alias="collectionId")
    summary_id: int = Field(serialization_alias="summaryId")
    position: int | None = None
    created_at: str = Field(serialization_alias="createdAt")


class CollectionItemsResponse(BaseModel):
    items: list[CollectionItem]
    pagination: PaginationInfo


class CollectionAclEntry(BaseModel):
    user_id: int | None = Field(default=None, serialization_alias="userId")
    role: Literal["owner", "editor", "viewer"]
    status: Literal["active", "pending", "revoked"]
    invited_by: int | None = Field(default=None, serialization_alias="invitedBy")
    created_at: str | None = Field(default=None, serialization_alias="createdAt")
    updated_at: str | None = Field(default=None, serialization_alias="updatedAt")


class CollectionAclResponse(BaseModel):
    acl: list[CollectionAclEntry]


class CollectionInviteResponse(BaseModel):
    token: str
    role: Literal["editor", "viewer"]
    expires_at: str | None = Field(default=None, serialization_alias="expiresAt")


class CollectionIncomingInvite(BaseModel):
    id: int
    token: str
    role: Literal["editor", "viewer"]
    status: Literal["pending", "expired"]
    collection: CollectionResponse
    invited_by: int = Field(serialization_alias="invitedBy")
    created_at: str = Field(serialization_alias="createdAt")
    expires_at: str | None = Field(default=None, serialization_alias="expiresAt")


class CollectionIncomingInvitesResponse(BaseModel):
    invites: list[CollectionIncomingInvite]
    pagination: PaginationInfo | None = None


class CollectionMoveResponse(BaseModel):
    id: int
    parent_id: int | None = Field(serialization_alias="parentId")
    position: int
    server_version: int | None = Field(default=None, serialization_alias="serverVersion")
    updated_at: str = Field(serialization_alias="updatedAt")


class CollectionItemsMoveResponse(BaseModel):
    moved_summary_ids: list[int] = Field(serialization_alias="movedSummaryIds")


CollectionResponse.model_rebuild()
