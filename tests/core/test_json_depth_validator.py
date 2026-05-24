"""Security-oriented JSON structure validation tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.core.json_depth_validator import (
    JSONValidationError,
    calculate_json_depth,
    safe_json_parse,
    validate_json_structure,
)


def test_calculate_json_depth_handles_scalars_and_nested_containers() -> None:
    assert calculate_json_depth("value") == 0
    assert calculate_json_depth({}) == 0
    assert calculate_json_depth([]) == 0
    assert calculate_json_depth({"a": [{"b": 1}]}) == 3


def test_calculate_json_depth_raises_when_recursion_guard_is_exceeded() -> None:
    with pytest.raises(JSONValidationError, match="JSON depth exceeds maximum"):
        calculate_json_depth({"a": {"b": 1}}, max_depth=1)


def test_validate_json_structure_reports_depth_array_and_dict_limits() -> None:
    assert validate_json_structure(
        {"a": [1, 2]}, max_depth=5, max_array_length=3, max_dict_keys=3
    ) == (True, None)

    valid, error = validate_json_structure({"a": {"b": {"c": 1}}}, max_depth=2)
    assert valid is False
    assert error == "JSON depth exceeds maximum (2)"

    valid, error = validate_json_structure({"a": [1, 2, 3]}, max_array_length=2)
    assert valid is False
    assert error == "Array at root.a has 3 items, exceeds maximum (2)"

    valid, error = validate_json_structure({"a": 1, "b": 2}, max_dict_keys=1)
    assert valid is False
    assert error == "Dictionary at root has 2 keys, exceeds maximum (1)"


def test_validate_json_structure_handles_recursion_and_unexpected_errors() -> None:
    recursive: list[object] = []
    recursive.append(recursive)

    valid, error = validate_json_structure(recursive, max_depth=1000)
    assert valid is False
    assert error == "JSON structure too deeply nested (recursion limit)"

    with patch(
        "app.core.json_depth_validator.calculate_json_depth",
        side_effect=RuntimeError("validator failed"),
    ):
        valid, error = validate_json_structure({"a": 1})

    assert valid is False
    assert error == "Unexpected error during validation: validator failed"


def test_safe_json_parse_checks_size_syntax_and_structure() -> None:
    parsed, error = safe_json_parse(
        '{"items":[1,2]}', max_size=100, max_depth=3, max_array_length=2, max_dict_keys=2
    )
    assert parsed == {"items": [1, 2]}
    assert error is None

    parsed, error = safe_json_parse("{}", max_size=1)
    assert parsed is None
    assert error == "JSON size (2 bytes) exceeds maximum (1 bytes)"

    parsed, error = safe_json_parse("{bad json")
    assert parsed is None
    assert error == "Invalid JSON: Expecting property name enclosed in double quotes at position 1"

    parsed, error = safe_json_parse('{"items":[1,2,3]}', max_array_length=2)
    assert parsed is None
    assert error == "Array at root.items has 3 items, exceeds maximum (2)"
