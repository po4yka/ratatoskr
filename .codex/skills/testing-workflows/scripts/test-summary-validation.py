#!/usr/bin/env python3
"""Exercise strict provider-schema validation and compatibility shaping."""

import sys
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

    from jsonschema import Draft202012Validator

    from app.core.summary_contract import get_summary_json_schema, validate_and_shape_summary
    from app.core.summary_schema import SummaryModel

    payload = SummaryModel(
        summary_250="Short summary.",
        summary_1000="Longer summary with details.",
        tldr="TLDR version.",
    ).model_dump(mode="json")
    validator = Draft202012Validator(get_summary_json_schema())

    assert not list(validator.iter_errors(payload))
    assert list(validator.iter_errors({"summary_250": "Incomplete"}))

    shaped = validate_and_shape_summary(
        {
            "summary_250": "Short summary.",
            "summary_1000": "Longer summary with details.",
            "tldr": "TLDR version.",
        }
    )
    assert set(shaped) == set(SummaryModel.model_fields)
    print("Strict schema rejection and compatibility shaping verified.")


if __name__ == "__main__":
    main()
