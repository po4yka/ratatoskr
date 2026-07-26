#!/usr/bin/env python3
"""Run Ratatoskr's tolerant compatibility shaping on a summary payload."""

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Shape legacy/tolerant summary JSON through the compatibility mapper."
    )
    parser.add_argument("filename", type=Path, help="Path to the summary JSON file")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from app.core.summary_contract import validate_and_shape_summary

    data = json.loads(args.filename.read_text())
    try:
        shaped = validate_and_shape_summary(data)
    except Exception as exc:
        print(f"Compatibility shaping failed: {exc}")
        return 1

    print("Compatibility shaping succeeded.")
    print(f"  fields: {len(shaped)}")
    print(f"  summary_250: {len(shaped['summary_250'])} chars")
    print(f"  summary_1000: {len(shaped['summary_1000'])} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
