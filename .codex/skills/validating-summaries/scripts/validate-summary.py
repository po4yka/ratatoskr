#!/usr/bin/env python3
"""Validate raw model output against the strict provider JSON schema."""

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate raw summary JSON against the strict provider schema."
    )
    parser.add_argument("filename", type=Path, help="Path to the summary JSON file")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from jsonschema import Draft202012Validator

    from app.core.summary_contract import get_summary_json_schema

    data = json.loads(args.filename.read_text())
    validator = Draft202012Validator(get_summary_json_schema())
    errors = sorted(validator.iter_errors(data), key=lambda error: list(error.absolute_path))
    if errors:
        for error in errors:
            path = ".".join(str(part) for part in error.absolute_path) or "<root>"
            print(f"ERROR {path}: {error.message}")
        return 1

    print("Strict provider-schema validation complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
