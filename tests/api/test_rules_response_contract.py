"""The rules API must not present inert fields as working ones.

Rules are stored, listed and edited; nothing executes them. runCount is
permanently 0 and lastTriggeredAt permanently null, so an API consumer reading
them without a caveat sees a rule that has not matched yet rather than a feature
that does not run.

The OpenAPI drift check does not cover this on its own: dropping the description
and regenerating changes both files together and passes. This asserts the
description exists at the source.
"""

from __future__ import annotations

import pytest

from app.api.models.responses.rules import RuleResponse

_CAVEAT = "not executed"


@pytest.mark.parametrize("field", ["run_count", "last_triggered_at"])
def test_the_inert_fields_say_so(field: str) -> None:
    description = RuleResponse.model_fields[field].description or ""
    assert _CAVEAT in description, (
        f"{field} is permanently inert and its description does not say so"
    )


def test_the_caveat_reaches_the_generated_contract() -> None:
    """A description that never reaches OpenAPI helps nobody outside this repo."""
    from pathlib import Path

    contract = (Path(__file__).resolve().parents[2] / "docs/openapi/mobile_api.yaml").read_text(
        encoding="utf-8"
    )
    assert _CAVEAT in contract, "run `make generate-openapi` -- the caveat is not in the contract"


def test_the_working_fields_are_left_alone() -> None:
    """Only the execution-shaped fields are inert; the rest describe real state."""
    for field in ("id", "name", "enabled", "event_type", "conditions", "actions"):
        description = RuleResponse.model_fields[field].description or ""
        assert _CAVEAT not in description, f"{field} is real state, not an inert field"
