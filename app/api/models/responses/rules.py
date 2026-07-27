"""Rules API response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .common import AliasCompatibleResponseModel


class RuleResponse(AliasCompatibleResponseModel):
    id: int
    name: str
    description: str | None = None
    enabled: bool
    event_type: str = Field(serialization_alias="eventType")
    match_mode: str = Field(serialization_alias="matchMode")
    conditions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    priority: int
    run_count: int = Field(serialization_alias="runCount")
    last_triggered_at: str | None = Field(default=None, serialization_alias="lastTriggeredAt")
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")


class RuleLogResponse(AliasCompatibleResponseModel):
    id: int
    rule_id: int = Field(serialization_alias="ruleId")
    summary_id: int | None = Field(default=None, serialization_alias="summaryId")
    event_type: str = Field(serialization_alias="eventType")
    matched: bool
    conditions_result: list[dict[str, Any]] | None = Field(
        default=None, serialization_alias="conditionsResult"
    )
    actions_taken: list[dict[str, Any]] | None = Field(
        default=None, serialization_alias="actionsTaken"
    )
    error: str | None = None
    duration_ms: int | None = Field(default=None, serialization_alias="durationMs")
    created_at: str = Field(serialization_alias="createdAt")


class RuleListResponse(BaseModel):
    rules: list[RuleResponse]


class RuleDeletionResponse(BaseModel):
    deleted: bool
    id: int


class RuleDryRunResponse(BaseModel):
    matched: bool
    conditions_result: list[dict[str, Any]]
    would_execute_actions: list[dict[str, Any]]


class RuleLogListResponse(BaseModel):
    logs: list[RuleLogResponse]
