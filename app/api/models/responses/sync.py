# ruff: noqa: TC001
"""Sync API response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, PrivateAttr, field_serializer

from .common import PaginationInfo


class SyncSessionData(BaseModel):
    session_id: str
    expires_at: str
    default_limit: int
    max_limit: int
    last_issued_since: int | None = None


class SyncEntityEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    entity_type: str
    id: int | str
    server_version: int
    updated_at: str
    deleted_at: str | None = None
    summary: dict[str, Any] | None = None
    request: dict[str, Any] | None = None
    preference: dict[str, Any] | None = None
    stat: dict[str, Any] | None = None
    crawl_result: dict[str, Any] | None = None
    llm_call: dict[str, Any] | None = None
    highlight: dict[str, Any] | None = None
    tag: dict[str, Any] | None = None
    summary_tag: dict[str, Any] | None = None

    # Internal-only creation time in epoch-ms, populated by SyncRecordCollector.
    # Delta sync uses it to split the created vs updated buckets: a row created
    # at/before the client cursor was already seen, so a change is an update; one
    # created after the cursor is new to the client. A PrivateAttr so it is never
    # serialized to the wire and never appears in the OpenAPI schema.
    _created_at_ms: int | None = PrivateAttr(default=None)

    @field_serializer("id")
    def serialize_id(self, value: int | str) -> str:
        return str(value)


class FullSyncResponseData(BaseModel):
    session_id: str
    has_more: bool
    next_since: int | None = None
    items: list[SyncEntityEnvelope]
    pagination: PaginationInfo


class DeltaSyncResponseData(BaseModel):
    session_id: str
    since: int
    has_more: bool
    next_since: int | None = None
    created: list[SyncEntityEnvelope]
    updated: list[SyncEntityEnvelope]
    deleted: list[SyncEntityEnvelope]


class SyncApplyItemResult(BaseModel):
    entity_type: str
    id: int | str
    status: Literal["applied", "conflict", "invalid"]
    server_version: int | None = None
    server_snapshot: SyncEntityEnvelope | None = None
    error_code: str | None = None
    message: str | None = None

    @field_serializer("id")
    def serialize_id(self, value: int | str) -> str:
        return str(value)


class SyncApplyResponseData(BaseModel):
    session_id: str
    results: list[SyncApplyItemResult]
    conflicts: list[SyncApplyItemResult] | None = None
    has_more: bool | None = None
