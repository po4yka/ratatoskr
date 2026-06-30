#!/usr/bin/env python3
"""Validate a summary JSON file using the project's own validation utilities.

Requires the project to be importable (run from repo root with venv active).
"""

import argparse
import json
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a summary JSON file using project utilities."
    )
    parser.add_argument("filename", help="Path to the summary JSON file")
    args = parser.parse_args()

    from app.core.summary_contract import validate_and_shape_summary

    with open(args.filename) as f:
        data = json.load(f)

    try:
        validated = validate_and_shape_summary(data)
        print("Summary valid!")
        print(f"  summary_250: {len(validated['summary_250'])} chars")
        print(f"  summary_1000: {len(validated['summary_1000'])} chars")
        print(f"  topic_tags: {len(validated['topic_tags'])} tags")
    except Exception as e:
        print(f"Validation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
