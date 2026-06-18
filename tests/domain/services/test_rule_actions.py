# ruff: noqa: RUF059
"""Tests for rule engine validation functions.

Covers validate_event_type, validate_condition, validate_action, and
validate_rule.  The condition *evaluator* is tested separately in
test_rule_conditions.py.
"""

from __future__ import annotations

import pytest

from app.domain.services.rule_engine import (
    MAX_ACTIONS_PER_RULE,
    MAX_CONDITIONS_PER_RULE,
    MAX_RULES_PER_USER,
    VALID_ACTION_TYPES,
    VALID_CONDITION_TYPES,
    VALID_EVENT_TYPES,
    validate_action,
    validate_condition,
    validate_event_type,
    validate_rule,
)

# ---------------------------------------------------------------------------
# validate_event_type
# ---------------------------------------------------------------------------


class TestValidateEventType:
    def test_valid_event_types(self) -> None:
        for et in VALID_EVENT_TYPES:
            valid, err = validate_event_type(et)
            assert valid, f"{et} should be valid"
            assert err is None

    def test_invalid_event_type(self) -> None:
        valid, err = validate_event_type("nonexistent.event")
        assert not valid
        assert err is not None
        assert "nonexistent.event" in err

    def test_empty_string(self) -> None:
        valid, err = validate_event_type("")
        assert not valid


# ---------------------------------------------------------------------------
# validate_condition
# ---------------------------------------------------------------------------


class TestValidateCondition:
    def test_valid_condition_for_each_type(self) -> None:
        for cond_type in VALID_CONDITION_TYPES:
            cond = {"type": cond_type, "operator": "contains", "value": "test"}
            valid, err = validate_condition(cond)
            assert valid, f"condition type '{cond_type}' should be valid"
            assert err is None

    def test_invalid_type(self) -> None:
        cond = {"type": "invalid_type", "operator": "equals", "value": "test"}
        valid, err = validate_condition(cond)
        assert not valid
        assert "invalid_type" in (err or "")

    def test_missing_type(self) -> None:
        valid, err = validate_condition({"operator": "equals", "value": "test"})
        assert not valid

    def test_missing_operator(self) -> None:
        cond = {"type": "domain_matches", "value": "example.com"}
        valid, err = validate_condition(cond)
        assert not valid
        assert "operator" in (err or "")

    def test_missing_value(self) -> None:
        cond = {"type": "domain_matches", "operator": "contains"}
        valid, err = validate_condition(cond)
        assert not valid
        assert "value" in (err or "")

    def test_empty_dict(self) -> None:
        valid, err = validate_condition({})
        assert not valid


# ---------------------------------------------------------------------------
# validate_action
# ---------------------------------------------------------------------------


class TestValidateAction:
    def test_valid_action_for_each_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "app.domain.services.rule_engine.validate_webhook_url",
            lambda _url: (True, None),
        )

        for action_type in VALID_ACTION_TYPES:
            action = {"type": action_type, "params": {"key": "value"}}
            if action_type == "send_webhook":
                action["params"] = {"url": "https://example.com/hook"}
            valid, err = validate_action(action)
            assert valid, f"action type '{action_type}' should be valid"
            assert err is None

    def test_valid_add_tag(self) -> None:
        action = {"type": "add_tag", "params": {"tag_name": "test"}}
        valid, err = validate_action(action)
        assert valid

    def test_valid_send_webhook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "app.domain.services.rule_engine.validate_webhook_url",
            lambda _url: (True, None),
        )

        action = {"type": "send_webhook", "params": {"url": "https://example.com/hook"}}
        valid, err = validate_action(action)
        assert valid

    def test_send_webhook_requires_url(self) -> None:
        action = {"type": "send_webhook", "params": {}}

        valid, err = validate_action(action)

        assert not valid
        assert "params.url" in (err or "")

    def test_send_webhook_rejects_unsafe_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "app.domain.services.rule_engine.validate_webhook_url",
            lambda _url: (False, "private IP address"),
        )

        action = {"type": "send_webhook", "params": {"url": "http://127.0.0.1/hook"}}

        valid, err = validate_action(action)

        assert not valid
        assert "invalid send_webhook URL" in (err or "")
        assert "private IP address" in (err or "")

    def test_invalid_type(self) -> None:
        action = {"type": "invalid_action", "params": {}}
        valid, err = validate_action(action)
        assert not valid
        assert "invalid_action" in (err or "")

    def test_missing_type(self) -> None:
        valid, err = validate_action({"params": {}})
        assert not valid

    def test_missing_params(self) -> None:
        action = {"type": "add_tag"}
        valid, err = validate_action(action)
        assert not valid
        assert "params" in (err or "")

    def test_params_not_dict(self) -> None:
        action = {"type": "add_tag", "params": "not_a_dict"}
        valid, err = validate_action(action)
        assert not valid

    def test_empty_dict(self) -> None:
        valid, err = validate_action({})
        assert not valid


# ---------------------------------------------------------------------------
# validate_rule
# ---------------------------------------------------------------------------


class TestValidateRule:
    @staticmethod
    def _condition(value: str = "test") -> dict:
        return {"type": "domain_matches", "operator": "contains", "value": value}

    @staticmethod
    def _action(tag: str = "auto") -> dict:
        return {"type": "add_tag", "params": {"tag_name": tag}}

    def test_valid_rule(self) -> None:
        valid, err = validate_rule(
            "summary.created",
            [self._condition()],
            [self._action()],
            "all",
        )
        assert valid
        assert err is None

    def test_valid_rule_any_mode(self) -> None:
        valid, err = validate_rule(
            "summary.created",
            [self._condition()],
            [self._action()],
            "any",
        )
        assert valid

    def test_invalid_event_type(self) -> None:
        valid, err = validate_rule(
            "bad.event",
            [self._condition()],
            [self._action()],
            "all",
        )
        assert not valid

    def test_invalid_match_mode(self) -> None:
        valid, err = validate_rule(
            "summary.created",
            [self._condition()],
            [self._action()],
            "none",
        )
        assert not valid
        assert "match_mode" in (err or "")

    def test_too_many_conditions(self) -> None:
        conditions = [self._condition(f"test{i}") for i in range(MAX_CONDITIONS_PER_RULE + 1)]
        valid, err = validate_rule(
            "summary.created",
            conditions,
            [self._action()],
            "all",
        )
        assert not valid
        assert "conditions" in (err or "").lower()

    def test_too_many_actions(self) -> None:
        actions = [self._action(f"tag{i}") for i in range(MAX_ACTIONS_PER_RULE + 1)]
        valid, err = validate_rule(
            "summary.created",
            [self._condition()],
            actions,
            "all",
        )
        assert not valid
        assert "actions" in (err or "").lower()

    def test_invalid_condition_propagates(self) -> None:
        valid, err = validate_rule(
            "summary.created",
            [{"type": "bad_type", "operator": "eq", "value": "x"}],
            [self._action()],
            "all",
        )
        assert not valid

    def test_invalid_action_propagates(self) -> None:
        valid, err = validate_rule(
            "summary.created",
            [self._condition()],
            [{"type": "bad_action", "params": {}}],
            "all",
        )
        assert not valid

    def test_invalid_send_webhook_url_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "app.domain.services.rule_engine.validate_webhook_url",
            lambda _url: (False, "private IP address"),
        )

        valid, err = validate_rule(
            "summary.created",
            [self._condition()],
            [{"type": "send_webhook", "params": {"url": "http://127.0.0.1/hook"}}],
            "all",
        )

        assert not valid
        assert "invalid send_webhook URL" in (err or "")

    def test_empty_conditions_and_actions(self) -> None:
        valid, err = validate_rule("summary.created", [], [], "all")
        assert valid

    def test_constants_reasonable(self) -> None:
        assert MAX_RULES_PER_USER >= 10
        assert MAX_ACTIONS_PER_RULE >= 5
        assert MAX_CONDITIONS_PER_RULE >= 3
