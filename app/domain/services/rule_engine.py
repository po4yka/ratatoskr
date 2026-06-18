"""Pure domain service for automation rule evaluation.

These functions contain no DB access -- they operate on values only.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.logging_utils import get_logger
from app.domain.services.webhook_service import validate_webhook_url

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Valid type enums
# ---------------------------------------------------------------------------

VALID_EVENT_TYPES = frozenset(
    {
        "summary.created",
        "summary.updated",
        "request.completed",
        "request.failed",
        "tag.attached",
        "tag.detached",
        "collection.item_added",
    }
)

VALID_CONDITION_TYPES = frozenset(
    {
        "domain_matches",
        "title_contains",
        "has_tag",
        "language_is",
        "reading_time",
        "source_type",
        "content_contains",
    }
)

VALID_ACTION_TYPES = frozenset(
    {
        "add_tag",
        "remove_tag",
        "add_to_collection",
        "remove_from_collection",
        "archive",
        "set_favorite",
        "send_webhook",
    }
)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_RULES_PER_USER = 50
MAX_ACTIONS_PER_RULE = 10
MAX_CONDITIONS_PER_RULE = 5
MAX_EXECUTIONS_PER_MINUTE = 100

# ---------------------------------------------------------------------------
# Validation functions
# ---------------------------------------------------------------------------


def validate_event_type(event_type: str) -> tuple[bool, str | None]:
    """Validate that *event_type* is a recognised event."""
    if event_type not in VALID_EVENT_TYPES:
        return False, f"invalid event type: {event_type}"
    return True, None


def validate_condition(condition: dict[str, Any]) -> tuple[bool, str | None]:
    """Validate a single condition dict.

    Must contain ``type`` (in *VALID_CONDITION_TYPES*), ``operator``, and
    ``value``.
    """
    cond_type = condition.get("type")
    if not cond_type or cond_type not in VALID_CONDITION_TYPES:
        return False, f"invalid condition type: {cond_type}"
    if "operator" not in condition:
        return False, "condition must have an 'operator' field"
    if "value" not in condition:
        return False, "condition must have a 'value' field"
    return True, None


def validate_action(action: dict[str, Any]) -> tuple[bool, str | None]:
    """Validate a single action dict.

    Must contain ``type`` (in *VALID_ACTION_TYPES*) and a ``params`` dict.
    """
    action_type = action.get("type")
    if not action_type or action_type not in VALID_ACTION_TYPES:
        return False, f"invalid action type: {action_type}"
    params = action.get("params")
    if not isinstance(params, dict):
        return False, "action must have a 'params' dict"
    if action_type == "send_webhook":
        url = str(params.get("url", "")).strip()
        if not url:
            return False, "send_webhook action requires params.url"
        valid_url, url_error = validate_webhook_url(url)
        if not valid_url:
            return False, f"invalid send_webhook URL: {url_error}"
    return True, None


def validate_rule(
    event_type: str,
    conditions: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    match_mode: str,
) -> tuple[bool, str | None]:
    """Combined validation for a complete rule definition."""
    ok, err = validate_event_type(event_type)
    if not ok:
        return False, err

    if match_mode not in {"all", "any"}:
        return False, f"match_mode must be 'all' or 'any', got: {match_mode}"

    if len(conditions) > MAX_CONDITIONS_PER_RULE:
        return False, f"too many conditions (max {MAX_CONDITIONS_PER_RULE})"

    if len(actions) > MAX_ACTIONS_PER_RULE:
        return False, f"too many actions (max {MAX_ACTIONS_PER_RULE})"

    for cond in conditions:
        ok, err = validate_condition(cond)
        if not ok:
            return False, err

    for act in actions:
        ok, err = validate_action(act)
        if not ok:
            return False, err

    return True, None


# ---------------------------------------------------------------------------
# Condition evaluator
# ---------------------------------------------------------------------------


class RuleConditionEvaluator:
    """Evaluates rule conditions against a summary context."""

    @staticmethod
    def evaluate_conditions(
        conditions: list[dict[str, Any]],
        context: dict[str, Any],
        match_mode: str = "all",
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Return *(overall_matched, per_condition_results)*."""
        results: list[dict[str, Any]] = []
        for cond in conditions:
            matched = RuleConditionEvaluator._evaluate_single(cond, context)
            results.append({"condition": cond, "matched": matched})

        if match_mode == "any":
            return any(r["matched"] for r in results), results
        return all(r["matched"] for r in results), results

    # -- dispatch -----------------------------------------------------------

    @staticmethod
    def _evaluate_single(condition: dict[str, Any], context: dict[str, Any]) -> bool:
        cond_type = condition.get("type", "")
        evaluator = {
            "domain_matches": RuleConditionEvaluator._domain_matches,
            "title_contains": RuleConditionEvaluator._title_contains,
            "has_tag": RuleConditionEvaluator._has_tag,
            "language_is": RuleConditionEvaluator._language_is,
            "reading_time": RuleConditionEvaluator._reading_time,
            "source_type": RuleConditionEvaluator._source_type,
            "content_contains": RuleConditionEvaluator._content_contains,
        }.get(cond_type)
        if not evaluator:
            return False
        return evaluator(condition, context)

    # -- individual evaluators ----------------------------------------------

    @staticmethod
    def _domain_matches(cond: dict[str, Any], ctx: dict[str, Any]) -> bool:
        url = ctx.get("url", "")
        value = cond.get("value", "")
        operator = cond.get("operator", "")
        if operator == "equals":
            return bool(url == value)
        if operator == "contains":
            return bool(value in url)
        if operator == "regex":
            return _regex_search(value, url)
        return False

    @staticmethod
    def _title_contains(cond: dict[str, Any], ctx: dict[str, Any]) -> bool:
        title = ctx.get("title", "")
        value = cond.get("value", "")
        operator = cond.get("operator", "")
        if operator == "contains":
            return value.lower() in title.lower()
        if operator == "regex":
            return _regex_search(value, title)
        return False

    @staticmethod
    def _has_tag(cond: dict[str, Any], ctx: dict[str, Any]) -> bool:
        tags: list[str] = ctx.get("tags", [])
        value = cond.get("value", [])
        operator = cond.get("operator", "")
        if not isinstance(value, list):
            value = [value]
        if operator == "any":
            return bool(set(value) & set(tags))
        if operator == "all":
            return set(value).issubset(set(tags))
        if operator == "none":
            return not (set(value) & set(tags))
        return False

    @staticmethod
    def _language_is(cond: dict[str, Any], ctx: dict[str, Any]) -> bool:
        language = ctx.get("language", "")
        value = cond.get("value", "")
        operator = cond.get("operator", "")
        if operator == "equals":
            return bool(language == value)
        if operator == "in":
            if isinstance(value, list):
                return bool(language in value)
            return bool(language in [v.strip() for v in value.split(",")])
        return False

    @staticmethod
    def _reading_time(cond: dict[str, Any], ctx: dict[str, Any]) -> bool:
        reading_time = ctx.get("reading_time", 0)
        try:
            value = int(cond.get("value", 0))
        except (TypeError, ValueError):
            return False
        operator = cond.get("operator", "")
        if operator == "gt":
            return bool(reading_time > value)
        if operator == "lt":
            return bool(reading_time < value)
        if operator == "eq":
            return bool(reading_time == value)
        return False

    @staticmethod
    def _source_type(cond: dict[str, Any], ctx: dict[str, Any]) -> bool:
        source_type = ctx.get("source_type", "")
        value = cond.get("value", "")
        operator = cond.get("operator", "")
        if operator == "equals":
            return bool(source_type == value)
        if operator == "in":
            if isinstance(value, list):
                return bool(source_type in value)
            return bool(source_type in [v.strip() for v in value.split(",")])
        return False

    @staticmethod
    def _content_contains(cond: dict[str, Any], ctx: dict[str, Any]) -> bool:
        content = ctx.get("content", "")
        value = cond.get("value", "")
        operator = cond.get("operator", "")
        if operator == "contains":
            return value.lower() in content.lower()
        if operator == "regex":
            return _regex_search(value, content)
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _regex_search(pattern: str, text: str) -> bool:
    """Return True if *pattern* matches anywhere in *text*.

    Invalid regex patterns are caught and logged; the result is ``False``.
    """
    try:
        return bool(re.search(pattern, text, re.IGNORECASE))
    except re.error:
        logger.warning("invalid regex pattern in rule condition", extra={"pattern": pattern})
        return False
