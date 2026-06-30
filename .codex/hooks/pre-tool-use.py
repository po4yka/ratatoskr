#!/usr/bin/env python3
"""Codex PreToolUse hook for Ratatoskr workspace safety."""

from __future__ import annotations

import json
import re
import sys
from typing import Any


PROTECTED_PATH_PATTERNS = (
    "data/ratatoskr.db",
    "requirements.txt",
    "requirements-dev.txt",
    ".git/",
)

DANGEROUS_PYTHON_PATTERNS = (
    ("os.system", "Direct shell command execution"),
    ("eval(", "Arbitrary code evaluation"),
    ("exec(", "Arbitrary code execution"),
    ("__import__", "Dynamic import"),
)

DANGEROUS_SHELL_PATTERNS = (
    (r"rm\s+-rf\s+/", "Recursive deletion from root"),
    (r"rm\s+-rf\s+\$HOME", "Deletion of home directory"),
    (r"rm\s+-rf\s+~", "Deletion of home directory"),
    (r"rm\s+-rf\s+\.\s*$", "Deletion of current directory"),
    (r"rm\s+-rf\s+/data", "Deletion of data directory"),
    (r">/dev/sd[a-z]", "Direct disk write"),
    (r"dd\s+if=.*of=/dev/", "Direct disk imaging"),
    (r"\bmkfs\b", "Filesystem creation"),
    (r"chmod\s+777", "Overly permissive permissions"),
    (r"curl.*\|\s*bash", "Piping curl to bash"),
    (r"wget.*\|\s*sh", "Piping wget to shell"),
)

WARNING_SHELL_PATTERNS = (
    (r"pip\s+install(?!.*-r\s+requirements)", "Installing packages outside requirements"),
    (r"docker\s+rm\s+-f", "Forcing container removal"),
    (r"git\s+push\s+-f", "Force pushing to git"),
    (r"drop\s+table", "Dropping database table"),
)


def load_input() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


def tool_input(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("tool_input") or data.get("tool_args") or {}
    return value if isinstance(value, dict) else {}


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def command_text(args: dict[str, Any]) -> str:
    command = args.get("command") or args.get("cmd") or ""
    return command if isinstance(command, str) else ""


def edited_file_path(args: dict[str, Any]) -> str:
    for key in ("file_path", "path", "filename"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def edited_content(args: dict[str, Any]) -> str:
    parts = []
    for key in ("new_string", "content", "patch"):
        value = args.get(key)
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)


def main() -> None:
    data = load_input()
    args = tool_input(data)
    file_path = edited_file_path(args)

    for pattern in PROTECTED_PATH_PATTERNS:
        if pattern in file_path:
            deny(f"Cannot modify protected file: {file_path} (matched {pattern}).")
            return

    if file_path.endswith(".py"):
        content = edited_content(args)
        for pattern, reason in DANGEROUS_PYTHON_PATTERNS:
            if pattern in content:
                print(
                    f"WARNING: Potentially dangerous Python pattern `{pattern}` in {file_path}: {reason}",
                    file=sys.stderr,
                )

    command = command_text(args)
    if command:
        for pattern, reason in DANGEROUS_SHELL_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                deny(f"Dangerous command blocked: {reason}. Command: {command}")
                return

        for pattern, reason in WARNING_SHELL_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                print(f"WARNING: Potentially risky operation: {reason}. Command: {command}", file=sys.stderr)


if __name__ == "__main__":
    main()
