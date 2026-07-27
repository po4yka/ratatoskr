"""Webhook API response models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .common import AliasCompatibleResponseModel


class WebhookSubscriptionResponse(AliasCompatibleResponseModel):
    id: int
    name: str | None = None
    url: str
    events: list[str]
    enabled: bool
    status: str
    secret_preview: str = Field(serialization_alias="secretPreview")
    failure_count: int = Field(serialization_alias="failureCount")
    last_delivery_at: str | None = Field(default=None, serialization_alias="lastDeliveryAt")
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")


class WebhookDeliveryResponse(AliasCompatibleResponseModel):
    id: int
    event_type: str = Field(serialization_alias="eventType")
    response_status: int | None = Field(default=None, serialization_alias="responseStatus")
    success: bool
    attempt: int
    duration_ms: int | None = Field(default=None, serialization_alias="durationMs")
    error: str | None = None
    created_at: str = Field(serialization_alias="createdAt")


class WebhookSubscriptionListResponse(BaseModel):
    subscriptions: list[WebhookSubscriptionResponse]


class WebhookCreatedResponse(WebhookSubscriptionResponse):
    secret: str


class WebhookDeletionResponse(BaseModel):
    deleted: bool
    id: int


class WebhookDeliveryListResponse(BaseModel):
    deliveries: list[WebhookDeliveryResponse]


class WebhookSecretResponse(BaseModel):
    id: int
    secret: str
