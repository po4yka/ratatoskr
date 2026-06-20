"""Automation rule management endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.dependencies.database import get_rule_repository, get_summary_repository
from app.api.exceptions import APIException, ErrorCode, ResourceNotFoundError, ValidationError
from app.api.models.requests import (  # noqa: TC001  # used at runtime in route body annotations
    CreateRuleRequest,
    TestRuleRequest,
    UpdateRuleRequest,
)
from app.api.models.responses import (
    PaginationInfo,
    RuleLogResponse,
    RuleResponse,
    success_response,
)
from app.api.routers.auth import get_current_user
from app.api.search_helpers import isotime
from app.core.logging_utils import get_logger
from app.domain.services.rule_engine import (
    MAX_RULES_PER_USER,
    RuleConditionEvaluator,
    validate_rule,
)

logger = get_logger(__name__)

router = APIRouter()


def _rule_to_response(rule: dict[str, Any]) -> RuleResponse:
    """Convert a rule dict to a RuleResponse."""
    return RuleResponse(
        id=rule["id"],
        name=rule["name"],
        description=rule.get("description"),
        enabled=rule["enabled"],
        event_type=rule["event_type"],
        match_mode=rule["match_mode"],
        conditions=rule.get("conditions_json") or [],
        actions=rule.get("actions_json") or [],
        priority=rule.get("priority", 0),
        run_count=rule.get("run_count", 0),
        last_triggered_at=isotime(rule.get("last_triggered_at")) or None,
        created_at=isotime(rule.get("created_at")),
        updated_at=isotime(rule.get("updated_at")),
    )


def _log_to_response(log: dict[str, Any]) -> RuleLogResponse:
    """Convert an execution log dict to a RuleLogResponse."""
    return RuleLogResponse(
        id=log["id"],
        rule_id=log["rule"],
        summary_id=log.get("summary"),
        event_type=log["event_type"],
        matched=log["matched"],
        conditions_result=log.get("conditions_result_json"),
        actions_taken=log.get("actions_taken_json"),
        error=log.get("error"),
        duration_ms=log.get("duration_ms"),
        created_at=isotime(log.get("created_at")),
    )


def _verify_rule_ownership(
    rule: dict[str, Any] | None, rule_id: int, user_id: int
) -> dict[str, Any]:
    """Verify that the rule exists and belongs to the user."""
    if rule is None or rule.get("is_deleted"):
        raise ResourceNotFoundError("AutomationRule", rule_id)
    if rule.get("user") != user_id and rule.get("user_id") != user_id:
        raise ResourceNotFoundError("AutomationRule", rule_id)
    return rule


@router.get("/")
async def list_rules(
    user: dict[str, Any] = Depends(get_current_user),
    rule_repo: Any = Depends(get_rule_repository),
) -> dict[str, Any]:
    """List all automation rules for the current user."""
    rules = await rule_repo.async_get_user_rules(user["user_id"])
    items = [_rule_to_response(r) for r in rules]
    return success_response({"rules": [i.model_dump(by_alias=True) for i in items]})


@router.post("/", status_code=201)
async def create_rule(
    body: CreateRuleRequest,
    user: dict[str, Any] = Depends(get_current_user),
    rule_repo: Any = Depends(get_rule_repository),
) -> dict[str, Any]:
    """Create a new automation rule."""
    ok, err = validate_rule(body.event_type, body.conditions, body.actions, body.match_mode)
    if not ok:
        raise ValidationError(err or "Invalid rule definition")

    existing = await rule_repo.async_get_user_rules(user["user_id"])
    if len(existing) >= MAX_RULES_PER_USER:
        raise APIException(
            message=f"Maximum of {MAX_RULES_PER_USER} rules per user",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )

    rule = await rule_repo.async_create_rule(
        user_id=user["user_id"],
        name=body.name,
        event_type=body.event_type,
        conditions=body.conditions,
        actions=body.actions,
        match_mode=body.match_mode,
        priority=body.priority,
        description=body.description,
    )
    return success_response(_rule_to_response(rule))


@router.get("/{rule_id}")
async def get_rule(
    rule_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    rule_repo: Any = Depends(get_rule_repository),
) -> dict[str, Any]:
    """Get rule details."""
    rule = await rule_repo.async_get_rule_by_id(rule_id)
    rule = _verify_rule_ownership(rule, rule_id, user["user_id"])
    return success_response(_rule_to_response(rule))


@router.patch("/{rule_id}")
async def update_rule(
    rule_id: int,
    body: UpdateRuleRequest,
    user: dict[str, Any] = Depends(get_current_user),
    rule_repo: Any = Depends(get_rule_repository),
) -> dict[str, Any]:
    """Update an automation rule."""
    rule = await rule_repo.async_get_rule_by_id(rule_id)
    rule = _verify_rule_ownership(rule, rule_id, user["user_id"])

    update_fields: dict[str, Any] = {}
    if body.name is not None:
        update_fields["name"] = body.name
    if body.description is not None:
        update_fields["description"] = body.description
    if body.enabled is not None:
        update_fields["enabled"] = body.enabled
    if body.event_type is not None:
        update_fields["event_type"] = body.event_type
    if body.match_mode is not None:
        update_fields["match_mode"] = body.match_mode
    if body.conditions is not None:
        update_fields["conditions"] = body.conditions
    if body.actions is not None:
        update_fields["actions"] = body.actions
    if body.priority is not None:
        update_fields["priority"] = body.priority

    if not update_fields:
        raise APIException(
            message="No fields to update",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )

    # Revalidate the full rule definition with merged values
    final_event_type = update_fields.get("event_type", rule["event_type"])
    final_conditions = update_fields.get("conditions", rule.get("conditions_json") or [])
    final_actions = update_fields.get("actions", rule.get("actions_json") or [])
    final_match_mode = update_fields.get("match_mode", rule["match_mode"])

    ok, err = validate_rule(final_event_type, final_conditions, final_actions, final_match_mode)
    if not ok:
        raise ValidationError(err or "Invalid rule definition")

    updated = await rule_repo.async_update_rule(rule_id, user["user_id"], **update_fields)
    return success_response(_rule_to_response(updated))


@router.delete("/{rule_id}")
async def delete_rule(
    rule_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    rule_repo: Any = Depends(get_rule_repository),
) -> dict[str, Any]:
    """Soft-delete an automation rule."""
    rule = await rule_repo.async_get_rule_by_id(rule_id)
    _verify_rule_ownership(rule, rule_id, user["user_id"])

    await rule_repo.async_soft_delete_rule(rule_id, user["user_id"])
    return success_response({"deleted": True, "id": rule_id})


@router.post("/{rule_id}/test")
async def dry_run_rule(
    rule_id: int,
    body: TestRuleRequest,
    user: dict[str, Any] = Depends(get_current_user),
    rule_repo: Any = Depends(get_rule_repository),
    summary_repo: Any = Depends(get_summary_repository),
) -> dict[str, Any]:
    """Dry-run a rule against a summary without side effects."""
    rule = await rule_repo.async_get_rule_by_id(rule_id)
    rule = _verify_rule_ownership(rule, rule_id, user["user_id"])

    summary_context = await summary_repo.async_get_summary_context_by_id(body.summary_id)
    if summary_context is None:
        raise ResourceNotFoundError("Summary", body.summary_id)
    summary = summary_context.get("summary") or {}
    request = summary_context.get("request") or {}
    if request.get("user_id") != user["user_id"]:
        raise ResourceNotFoundError("Summary", body.summary_id)

    # Build context from summary for condition evaluation
    summary_json = summary.get("json_payload") or {}
    if isinstance(summary_json, str):
        import json

        summary_json = json.loads(summary_json)

    context: dict[str, Any] = {
        "url": request.get("input_url", "") or "",
        "title": summary_json.get("title", "") if isinstance(summary_json, dict) else "",
        "tags": summary_json.get("topic_tags", []) if isinstance(summary_json, dict) else [],
        "language": summary.get("lang", "") or "",
        "reading_time": (
            summary_json.get("estimated_reading_time_min", 0)
            if isinstance(summary_json, dict)
            else 0
        ),
        "source_type": (
            summary_json.get("source_type", "") if isinstance(summary_json, dict) else ""
        ),
        "content": (summary_json.get("summary_1000", "") if isinstance(summary_json, dict) else ""),
    }

    conditions = rule.get("conditions_json") or []
    match_mode = rule.get("match_mode", "all")
    actions = rule.get("actions_json") or []

    matched, conditions_result = RuleConditionEvaluator.evaluate_conditions(
        conditions, context, match_mode
    )

    return success_response(
        {
            "matched": matched,
            "conditions_result": conditions_result,
            "would_execute_actions": actions if matched else [],
        }
    )


@router.get("/{rule_id}/logs")
async def list_execution_logs(
    rule_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    rule_repo: Any = Depends(get_rule_repository),
) -> dict[str, Any]:
    """Return paginated execution history for a rule."""
    rule = await rule_repo.async_get_rule_by_id(rule_id)
    _verify_rule_ownership(rule, rule_id, user["user_id"])

    logs = await rule_repo.async_get_execution_logs(rule_id, limit=limit, offset=offset)
    items = [_log_to_response(log) for log in logs]

    return success_response(
        {"logs": [i.model_dump(by_alias=True) for i in items]},
        pagination=PaginationInfo(
            total=len(items),
            limit=limit,
            offset=offset,
            has_more=len(items) == limit,
        ),
    )
