"""Characterization tests for the git argv builder (port of Engine.buildGitCommand).

The exact argv is the contract git executes, so every branch is pinned by fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.adapters.git_backup import git_commands

_FIXTURES = Path(__file__).parent / "fixtures"


def _load_json(*parts: str) -> Any:
    """Load a JSON fixture relative to this file's fixtures directory."""
    return json.loads(_FIXTURES.joinpath(*parts).read_text())


_CASES = _load_json("argv_cases.json")["cases"]


@pytest.mark.characterization
@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_build_git_command(case: dict[str, Any]) -> None:
    assert git_commands.build_git_command(**case["params"]) == case["expected_argv"]
