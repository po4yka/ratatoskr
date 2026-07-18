"""Sanitized public system status response models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .common import SuccessResponse


class PublicStatusLevel(StrEnum):
    """Stable public component and aggregate status values."""

    OPERATIONAL = "operational"
    DEGRADED = "degraded"
    OUTAGE = "outage"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


class PublicStatusComponent(BaseModel):
    """One sanitized public component check."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    status: PublicStatusLevel
    message: str | None = None
    checked_at: AwareDatetime
    latency_ms: float | None = Field(default=None, ge=0)


class PublicStatusGroup(BaseModel):
    """A stable group of related components."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    status: PublicStatusLevel
    components: list[PublicStatusComponent]


class PublicStatusSummary(BaseModel):
    """Exact component counts by public status."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    operational: int = Field(ge=0)
    degraded: int = Field(ge=0)
    outage: int = Field(ge=0)
    unknown: int = Field(ge=0)
    disabled: int = Field(ge=0)


class PublicStatusResponse(BaseModel):
    """Public status page payload."""

    model_config = ConfigDict(extra="forbid")

    status: PublicStatusLevel
    message: str
    generated_at: AwareDatetime
    refresh_after_seconds: int = Field(ge=1)
    summary: PublicStatusSummary
    groups: list[PublicStatusGroup]


class PublicStatusSuccessResponse(SuccessResponse):
    """Standard success envelope for the public status endpoint."""

    data: PublicStatusResponse
