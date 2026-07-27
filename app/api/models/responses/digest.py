# ruff: noqa: TC001
"""Digest API response models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.api.models.digest import (
    BulkOperationResponse,
    CategoryListResponse,
    CategoryResponse,
    ChannelMutationResponse,
    ChannelPostsListResponse,
    ChannelSubscriptionsResponse,
    DigestDeliveryListResponse,
    DigestPreferenceResponse,
    EmailAddressListResponse,
    EmailVerificationRequestResponse,
    EmailVerificationResponse,
    ResolveChannelResponse,
    StatusResponse,
    TriggerChannelDigestResponse,
    TriggerDigestResponse,
)

from .common import AliasCompatibleResponseModel, SuccessResponse


class CustomDigestResponse(AliasCompatibleResponseModel):
    id: str
    title: str | None = None
    content: str | None = None
    status: str
    created_at: str = Field(serialization_alias="createdAt")


class CustomDigestListResponse(BaseModel):
    digests: list[CustomDigestResponse]


class EmailDeliveryResponse(BaseModel):
    delivery_id: str
    status: str


class ChannelSubscriptionsSuccessResponse(SuccessResponse):
    data: ChannelSubscriptionsResponse


class ChannelMutationSuccessResponse(SuccessResponse):
    data: ChannelMutationResponse


class ResolveChannelSuccessResponse(SuccessResponse):
    data: ResolveChannelResponse


class ChannelPostsSuccessResponse(SuccessResponse):
    data: ChannelPostsListResponse


class BulkOperationSuccessResponse(SuccessResponse):
    data: BulkOperationResponse


class DigestPreferenceSuccessResponse(SuccessResponse):
    data: DigestPreferenceResponse


class EmailAddressListSuccessResponse(SuccessResponse):
    data: EmailAddressListResponse


class EmailVerificationRequestSuccessResponse(SuccessResponse):
    data: EmailVerificationRequestResponse


class EmailVerificationSuccessResponse(SuccessResponse):
    data: EmailVerificationResponse


class DigestDeliveryListSuccessResponse(SuccessResponse):
    data: DigestDeliveryListResponse


class TriggerDigestSuccessResponse(SuccessResponse):
    data: TriggerDigestResponse


class TriggerChannelDigestSuccessResponse(SuccessResponse):
    data: TriggerChannelDigestResponse


class CategoryListSuccessResponse(SuccessResponse):
    data: CategoryListResponse


class CategorySuccessResponse(SuccessResponse):
    data: CategoryResponse


class StatusSuccessResponse(SuccessResponse):
    data: StatusResponse


class CustomDigestSuccessResponse(SuccessResponse):
    data: CustomDigestResponse


class CustomDigestListSuccessResponse(SuccessResponse):
    data: CustomDigestListResponse


class EmailDeliverySuccessResponse(SuccessResponse):
    data: EmailDeliveryResponse
