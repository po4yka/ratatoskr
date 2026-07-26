"""Run a quick project-venv ruff check after Claude edits Python files."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def load_input() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


def main() -> None:
    data = load_input()
    args = data.get("tool_input") or data.get("tool_args") or {}
    if not isinstance(args, dict):
        return
    file_path = next(
        (
            value
            for key in ("file_path", "path", "filename")
            if isinstance((value := args.get(key)), str) and value
        ),
        "",
    )
    if not file_path.endswith(".py"):
        return
    if not os.path.exists(file_path) or any(
        part in file_path for part in ("venv", ".venv", "build", "dist")
    ):
        return

    root = Path(__file__).resolve().parents[2]
    venv_ruff = root / ".venv" / "bin" / "ruff"
    ruff = str(venv_ruff) if venv_ruff.is_file() else "ruff"
    print(f"Running quick lint on {file_path}...")
    try:
        result = subprocess.run(
            [ruff, "check", "--select", "F,E", file_path],
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

    if result.returncode != 0:
        print("WARNING: linting issues found:")
        print(result.stdout or result.stderr)
        print("Run `make format` for fixes or `make lint` for full analysis.")
    else:
        print("No critical linting issues detected.")


if __name__ == "__main__":
    main()
