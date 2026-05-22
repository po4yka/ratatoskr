"""Aggregation API response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .common import SuccessResponse


class AggregationFailurePayload(BaseModel):
    code: str | None = None
    message: str | None = None
    details: dict[str, Any] | None = None


class AggregationSourceItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    bundle_id: int = Field(alias="bundleId")
    source_item_id: str = Field(alias="sourceItemId")
    item_id: int | None = Field(default=None, alias="itemId")
    position: int
    original_url: str | None = Field(default=None, alias="originalUrl")
    normalized_url: str | None = Field(default=None, alias="normalizedUrl")
    source_kind: str = Field(alias="sourceKind")
    extraction_status: str = Field(alias="extractionStatus")
    title: str | None = None
    domain: str | None = None
    author: str | None = None
    published_at: str | None = Field(default=None, alias="publishedAt")
    error_code: str | None = Field(default=None, alias="errorCode")
    error_message: str | None = Field(default=None, alias="errorMessage")
    request_id: int | None = Field(default=None, alias="requestId")
    crawl_result_id: int | None = Field(default=None, alias="crawlResultId")
    summary_id: int | None = Field(default=None, alias="summaryId")
    duplicate_of_item_id: int | None = Field(default=None, alias="duplicateOfItemId")
    deleted: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class AggregationSourceBundle(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    bundle_id: int = Field(alias="bundleId")
    correlation_id: str | None = Field(default=None, alias="correlationId")
    status: str | None = None
    items: list[AggregationSourceItem] = Field(default_factory=list)


class AggregationSessionPayload(BaseModel):
    model_config = {"extra": "allow"}


class AggregationCreateData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session: dict[str, Any]
    aggregation: dict[str, Any]
    items: list[dict[str, Any]]
    source_bundle: AggregationSourceBundle = Field(alias="sourceBundle")


class AggregationDetailData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session: dict[str, Any]
    items: list[dict[str, Any]]
    aggregation: dict[str, Any] | None
    source_bundle: AggregationSourceBundle = Field(alias="sourceBundle")


class AggregationListData(BaseModel):
    sessions: list[dict[str, Any]]


class AggregationCreateResponse(SuccessResponse):
    data: AggregationCreateData


class AggregationDetailResponse(SuccessResponse):
    data: AggregationDetailData


class AggregationListResponse(SuccessResponse):
    data: AggregationListData


__all__ = [
    "AggregationCreateData",
    "AggregationCreateResponse",
    "AggregationDetailData",
    "AggregationDetailResponse",
    "AggregationFailurePayload",
    "AggregationListData",
    "AggregationListResponse",
    "AggregationSourceBundle",
    "AggregationSourceItem",
]
