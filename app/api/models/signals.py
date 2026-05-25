"""Pydantic models for signal feed API."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - required at runtime by Pydantic
from typing import Any

from pydantic import BaseModel, Field

from app.api.models.responses.common import SuccessResponse


class SignalFeedbackRequest(BaseModel):
    action: str = Field(pattern="^(like|dislike|skip|hide_source|queue|boost_topic)$")


class TopicPreferenceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    weight: float = Field(default=1.0, ge=0.0, le=5.0)


class SourceActiveRequest(BaseModel):
    is_active: bool


class SourceControlRequest(BaseModel):
    is_active: bool | None = None
    fetch_interval_seconds: int | None = Field(default=None, ge=300, le=604800)
    max_items_per_run: int | None = Field(default=None, ge=1, le=500)
    retry_policy: dict[str, object] | None = None


class SignalItemResponse(BaseModel):
    id: int | None = None
    user_id: int | None = None
    user: int | None = None
    feed_item_id: int | None = None
    feed_item: int | None = None
    topic_id: int | None = None
    topic: int | None = None
    status: str | None = None
    heuristic_score: float | None = None
    llm_score: float | None = None
    final_score: float | None = None
    evidence_json: dict[str, Any] | None = None
    filter_stage: str | None = None
    llm_judge_json: dict[str, Any] | None = None
    llm_cost_usd: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    feed_item_title: str | None = None
    feed_item_url: str | None = None
    source_kind: str | None = None
    source_title: str | None = None
    topic_name: str | None = None


class SignalSourceHealthResponse(BaseModel):
    id: int
    kind: str
    external_id: str | None = None
    url: str | None = None
    title: str | None = None
    is_active: bool
    fetch_error_count: int = 0
    last_error: str | None = None
    last_fetched_at: datetime | None = None
    last_successful_at: datetime | None = None
    last_failure_at: datetime | None = None
    subscription_id: int
    subscription_active: bool
    cadence_seconds: int | None = None
    next_fetch_at: datetime | None = None
    backoff_until: datetime | None = None
    fetch_interval_seconds: int | None = None
    max_items_per_run: int | None = None
    retry_policy: dict[str, Any] | None = None


class SignalVectorHealthResponse(BaseModel):
    ready: bool
    required: bool
    collection: str | None = None


class SignalHealthCountsResponse(BaseModel):
    total: int
    active: int
    errored: int


class SignalTopicResponse(BaseModel):
    id: int | None = None
    user_id: int | None = None
    user: int | None = None
    name: str | None = None
    description: str | None = None
    weight: float | None = None
    embedding_ref: str | None = None
    metadata_json: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SignalListData(BaseModel):
    signals: list[SignalItemResponse]


class SignalHealthData(BaseModel):
    vector: SignalVectorHealthResponse
    sources: SignalHealthCountsResponse


class SignalSourcesHealthData(BaseModel):
    sources: list[SignalSourceHealthResponse]


class SignalUpdatedData(BaseModel):
    updated: bool
    is_active: bool | None = None


class SignalQueuedData(BaseModel):
    queued: bool


class SignalTopicData(BaseModel):
    topic: SignalTopicResponse


class SignalListSuccessResponse(SuccessResponse):
    data: SignalListData


class SignalHealthSuccessResponse(SuccessResponse):
    data: SignalHealthData


class SignalSourcesHealthSuccessResponse(SuccessResponse):
    data: SignalSourcesHealthData


class SignalUpdatedSuccessResponse(SuccessResponse):
    data: SignalUpdatedData


class SignalQueuedSuccessResponse(SuccessResponse):
    data: SignalQueuedData


class SignalTopicSuccessResponse(SuccessResponse):
    data: SignalTopicData
