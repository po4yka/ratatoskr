#!/usr/bin/env python3
"""Run a quick ruff check after Codex edits Python files."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any


def load_input() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


def tool_input(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("tool_input") or data.get("tool_args") or {}
    return value if isinstance(value, dict) else {}


def edited_file_path(args: dict[str, Any]) -> str:
    for key in ("file_path", "path", "filename"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def main() -> None:
    file_path = edited_file_path(tool_input(load_input()))
    if not file_path.endswith(".py"):
        return
    if not os.path.exists(file_path) or any(part in file_path for part in ("venv", ".venv", "build", "dist")):
        return

    print(f"Running quick lint on {file_path}...")
    try:
        result = subprocess.run(
            ["ruff", "check", "--select", "F,E", file_path],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("WARNING: linting timed out")
        return
    except FileNotFoundError:
        print("WARNING: ruff not installed; skipping lint check")
        return

    if result.returncode != 0 and result.stdout:
        print("WARNING: linting issues found:")
        print(result.stdout)
        print("Run `make format` for formatting fixes or `make lint` for full analysis.")
    else:
        print("No critical linting issues detected.")


if __name__ == "__main__":
    main()
