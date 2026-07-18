#!/usr/bin/env python3
"""Run a quick ruff check after Codex edits Python files."""

from __future__ import annotations

import json
import os
import re
import shutil
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


def tool_input(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("tool_input") or data.get("tool_args") or {}
    if isinstance(value, dict):
        return value
    return {"patch": value} if isinstance(value, str) else {}


def edited_file_paths(args: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("file_path", "path", "filename"):
        value = args.get(key)
        if isinstance(value, str) and value:
            paths.append(value)

    edits = args.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                paths.extend(edited_file_paths(edit))

    for key in ("patch", "input"):
        patch = args.get(key)
        if isinstance(patch, str):
            paths.extend(
                match.group(1).strip()
                for match in re.finditer(
                    r"^\*\*\* (?:Add|Update|Delete) File: (.+)$",
                    patch,
                    re.MULTILINE,
                )
            )

    return list(dict.fromkeys(paths))


def project_root() -> Path:
    configured = os.environ.get("CODEX_PROJECT_DIR")
    if configured:
        return Path(configured)
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    return Path(result.stdout.strip()) if result.returncode == 0 else Path.cwd()


def main() -> None:
    root = project_root()
    file_paths: list[Path] = []
    for raw_path in edited_file_paths(tool_input(load_input())):
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        if path.suffix != ".py" or not path.exists():
            continue
        if any(part in {"venv", ".venv", "build", "dist"} for part in path.parts):
            continue
        file_paths.append(path)

    if not file_paths:
        return

    bundled_ruff = root / ".venv" / "bin" / "ruff"
    ruff_command = str(bundled_ruff) if bundled_ruff.is_file() else shutil.which("ruff")
    if not ruff_command:
        print("WARNING: ruff not installed; skipping lint check")
        return

    display_paths = [os.path.relpath(path, root) for path in file_paths]
    print(f"Running quick lint on {', '.join(display_paths)}...")
    try:
        result = subprocess.run(
            [ruff_command, "check", "--select", "F,E", *map(str, file_paths)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("WARNING: linting timed out")
        return

    if result.returncode != 0:
        print("WARNING: linting issues found:")
        print(result.stdout or result.stderr)
        print("Run `make format` for formatting fixes or `make lint` for full analysis.")
    else:
        print("No critical linting issues detected.")


if __name__ == "__main__":
    main()
