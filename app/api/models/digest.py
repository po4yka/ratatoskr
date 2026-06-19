"""Pydantic models for Digest Mini App API."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - required at runtime by Pydantic
from typing import Literal

from pydantic import BaseModel, Field


class ChannelSubscriptionResponse(BaseModel):
    """A channel subscription entry."""

    id: int
    username: str
    title: str | None = None
    is_active: bool
    fetch_error_count: int = 0
    last_error: str | None = None
    category_id: int | None = None
    category_name: str | None = None
    created_at: datetime


class SubscribeRequest(BaseModel):
    """Request to subscribe to a channel."""

    channel_username: str = Field(..., min_length=5, max_length=32)


class ChannelControlRequest(BaseModel):
    """Request to update per-channel ingestion controls."""

    is_active: bool | None = None
    fetch_interval_seconds: int | None = Field(None, ge=300, le=604800)
    max_items_per_run: int | None = Field(None, ge=1, le=500)
    retry_policy: dict[str, object] | None = None


class ResolveChannelRequest(BaseModel):
    """Request to resolve/preview a channel before subscribing."""

    channel_username: str = Field(..., min_length=5, max_length=32)


class ResolveChannelResponse(BaseModel):
    """Resolved channel metadata."""

    username: str
    title: str | None = None
    description: str | None = None
    member_count: int | None = None
    is_subscribed: bool = False


class DigestPreferenceResponse(BaseModel):
    """User digest preferences with source annotations."""

    delivery_time: str
    delivery_time_source: str  # "user" | "global"
    timezone: str
    timezone_source: str
    hours_lookback: int
    hours_lookback_source: str
    max_posts_per_digest: int
    max_posts_per_digest_source: str
    min_relevance_score: float
    min_relevance_score_source: str
    delivery_channel: Literal["telegram", "email"]
    delivery_channel_source: str
    email_address_id: int | None = None
    email_address_id_source: str


class UpdatePreferenceRequest(BaseModel):
    """Request to update digest preferences. Null fields keep current value."""

    delivery_time: str | None = Field(None, pattern=r"^\d{2}:\d{2}$")
    timezone: str | None = Field(None, max_length=50)
    hours_lookback: int | None = Field(None, ge=1, le=168)
    max_posts_per_digest: int | None = Field(None, ge=1, le=100)
    min_relevance_score: float | None = Field(None, ge=0.0, le=1.0)
    delivery_channel: Literal["telegram", "email"] | None = None
    email_address_id: int | None = Field(None, ge=1)


class EmailAddressResponse(BaseModel):
    """Verified or pending email address."""

    id: int
    email: str
    is_verified: bool
    verified_at: datetime | None = None
    created_at: datetime


class RequestEmailVerificationRequest(BaseModel):
    """Request a verification email for an address."""

    email: str = Field(..., min_length=3, max_length=256)


class VerifyEmailRequest(BaseModel):
    """Verify an email address with a one-time token."""

    token: str = Field(..., min_length=16, max_length=256)


class SendEmailRequest(BaseModel):
    """Request sending existing content to a verified email address."""

    email_address_id: int | None = Field(None, ge=1)


class DigestDeliveryResponse(BaseModel):
    """A digest delivery record."""

    id: int
    delivered_at: datetime
    post_count: int
    channel_count: int
    digest_type: str


class TriggerDigestResponse(BaseModel):
    """Response for on-demand digest trigger."""

    status: str = "queued"
    correlation_id: str


# --- Phase 2: Post preview models ---


class PostAnalysisResponse(BaseModel):
    """LLM analysis of a channel post."""

    real_topic: str | None = None
    tldr: str | None = None
    relevance_score: float | None = None
    content_type: str | None = None


class ChannelPostResponse(BaseModel):
    """A single post from a tracked channel."""

    message_id: int
    text: str
    date: datetime
    views: int | None = None
    forwards: int | None = None
    media_type: str | None = None
    url: str | None = None
    analysis: PostAnalysisResponse | None = None


class ChannelPostsListResponse(BaseModel):
    """Paginated list of channel posts."""

    posts: list[ChannelPostResponse]
    total: int
    channel_username: str


# --- Phase 3: Category models ---


class CategoryRequest(BaseModel):
    """Request to create or update a category."""

    name: str = Field(..., min_length=1, max_length=50)


class CategoryResponse(BaseModel):
    """A channel category."""

    id: int
    name: str
    position: int
    subscription_count: int = 0


class AssignCategoryRequest(BaseModel):
    """Request to assign a subscription to a category."""

    category_id: int | None = None


# --- Phase 4: Bulk operation models ---


class BulkUnsubscribeRequest(BaseModel):
    """Request to unsubscribe from multiple channels."""

    channel_usernames: list[str] = Field(..., min_length=1, max_length=50)


class BulkCategoryRequest(BaseModel):
    """Request to assign multiple subscriptions to a category."""

    subscription_ids: list[int] = Field(..., min_length=1, max_length=50)
    category_id: int | None = None


class BulkOperationResponse(BaseModel):
    """Response for bulk operations."""

    results: list[dict[str, str]]
    success_count: int
    error_count: int
