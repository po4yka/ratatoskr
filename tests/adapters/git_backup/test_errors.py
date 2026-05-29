"""Characterization tests for error classification (port of ErrorCategoryTest.kt).

Port of ``tests/test_error_category.py`` from the gitout standalone CLI.
JSON fixtures are co-located at ``tests/adapters/git_backup/fixtures/error_category/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.adapters.git_backup import errors
from app.adapters.git_backup.errors import ErrorCategory

_FIXTURES = Path(__file__).parent / "fixtures"


def _load_json(*parts: str) -> Any:
    """Load a JSON fixture relative to this test's fixtures directory."""
    return json.loads(_FIXTURES.joinpath(*parts).read_text())


_CLASSIFY = _load_json("error_category", "classify_cases.json")["cases"]
_PROPERTIES = _load_json("error_category", "properties.json")["categories"]


@pytest.mark.characterization
@pytest.mark.parametrize(
    "case",
    _CLASSIFY,
    ids=[c.get("note") or repr(c["message"])[:40] for c in _CLASSIFY],
)
def test_classify(case: dict) -> None:
    assert errors.classify(case["message"]) is ErrorCategory[case["expected"]]


@pytest.mark.characterization
@pytest.mark.parametrize("name", list(_PROPERTIES.keys()))
def test_category_properties(name: str) -> None:
    category = ErrorCategory[name]
    expected = _PROPERTIES[name]
    assert errors.is_retryable(category) is expected["is_retryable"]
    assert errors.should_use_http1_fallback(category) is expected["should_use_http1_fallback"]
    assert errors.delay_multiplier(category) == expected["delay_multiplier"]
    assert errors.display_name(category) == expected["display_name"]
    assert errors.suggestion(category) == expected["suggestion"]
