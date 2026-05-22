"""Pydantic models for signal feed API."""

from __future__ import annotations

from pydantic import BaseModel, Field


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
