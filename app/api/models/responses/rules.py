"""Rules API response models.

Rules are stored, listed and edited; nothing executes them. The engine was
removed in dd46ff80 after it turned out never to have run once -- its only
trigger was an event no production code ever published. The execution-shaped
fields below are documented as inert so an API consumer reads a zero as "nothing
runs rules" rather than "this rule has not matched yet".
"""

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
    run_count: int = Field(
        serialization_alias="runCount",
        description=(
            "Always 0: rules are stored but not executed. Kept so the field does "
            "not disappear from the contract if execution ever returns."
        ),
    )
    last_triggered_at: str | None = Field(
        default=None,
        serialization_alias="lastTriggeredAt",
        description="Always null: rules are stored but not executed.",
    )
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
