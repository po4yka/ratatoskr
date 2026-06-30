#!/usr/bin/env python3
"""Standalone summary JSON validator.

Checks required fields, character limits, topic tag format,
and entity structure against the summary contract.
"""

import argparse
import json
import sys

REQUIRED_FIELDS = [
    "summary_250",
    "summary_1000",
    "tldr",
    "key_ideas",
    "topic_tags",
    "entities",
    "estimated_reading_time_min",
    "key_stats",
    "answered_questions",
    "readability",
    "seo_keywords",
]


def validate(data: dict) -> list[str]:
    errors: list[str] = []

    # Check required fields
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        errors.append(f"Missing fields: {missing}")

    # Check character limits
    if "summary_250" in data and len(data["summary_250"]) > 250:
        errors.append(f"summary_250 is {len(data['summary_250'])} chars (max 250)")

    if "summary_1000" in data and len(data["summary_1000"]) > 1000:
        errors.append(f"summary_1000 is {len(data['summary_1000'])} chars (max 1000)")

    # Check topic tags format
    for tag in data.get("topic_tags", []):
        if not tag.startswith("#"):
            errors.append(f"Tag '{tag}' missing leading #")

    # Check entities structure
    entities = data.get("entities", {})
    for cat in ["people", "organizations", "locations"]:
        if cat in entities and not isinstance(entities[cat], list):
            errors.append(f"entities.{cat} must be a list")

    return errors


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a summary JSON file against the contract."
    )
    parser.add_argument("filename", help="Path to the summary JSON file")
    args = parser.parse_args()

    with open(args.filename) as f:
        data = json.load(f)

    errors = validate(data)
    if errors:
        for err in errors:
            print(f"ERROR: {err}")
        sys.exit(1)

    print("Validation complete!")


if __name__ == "__main__":
    main()
