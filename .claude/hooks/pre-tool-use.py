"""Claude PreToolUse hook for Ratatoskr workspace safety."""

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

# Blanket staging in a checkout that several sessions share. `git add -A` picks up
# whatever another session happens to have half-written, so an unrelated feature
# lands inside your commit under your subject line. That is not hypothetical: it
# is how f6f68b00 came to contain an entire star-list subsystem under a message
# about Pi container networks (see `git notes show f6f68b00`).
#
# Anchoring matters in both directions. The `git` must sit at a command position
# -- start of line, or after ; && || | -- so prose that merely mentions the
# command inside a heredoc or a quoted string is not blocked. And the flags are
# matched precisely: `--amend` must not read as "commit everything".
_CMD_START = r"(?:^|[;&|]\s*|\n\s*)"
# Something may sit between the command position and `git`. In this workspace the
# RTK hook rewrites `git add -A` into `rtk git add -A` before any other PreToolUse
# hook sees it, so a pattern anchored straight onto `git` matches nothing that is
# actually run. Only a known wrapper list is allowed through, not arbitrary words,
# so prose that merely mentions the command ("never use git add -A") stays clear.
_WRAPPER = r"(?:(?:rtk|sudo|command|time|nice|noglob|env)\s+|[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
_GIT = r"git(?:\s+-C\s+\S+)*\s+"

# The gap between the subcommand and the flag excludes quote characters, so a flag
# only counts when it appears before any quoted argument. Without that, a commit
# whose *message* mentions --all reads as a commit that passes --all.
_GAP = r"[^;&|\n\"']*"

BLANKET_STAGING_PATTERNS = (
    (
        rf"{_CMD_START}{_WRAPPER}{_GIT}add\s+(?:{_GAP}\s)?(?:-A\b|--all\b)",
        "`git add -A` / `--all` stages files this session did not touch",
    ),
    (
        rf"{_CMD_START}{_WRAPPER}{_GIT}add\s+(?:{_GAP}\s)?(?:\.|:/)(?:\s|$)",
        "`git add .` stages files this session did not touch",
    ),
    (
        rf"{_CMD_START}{_WRAPPER}{_GIT}commit\b{_GAP}(?:\s--all\b|\s(?<!-)-[a-zA-Z]*a[a-zA-Z]*\b)",
        "`git commit -a` stages every tracked modification, including other sessions'",
    ),
)

_BLANKET_STAGING_ADVICE = (
    "Stage the paths you actually changed instead: `git add path/one path/two`. "
    "Run `git status --short` first if you need the list. This checkout is worked "
    "on by more than one session at a time, so 'everything modified' is not the "
    "same set as 'everything I changed'."
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


def main() -> None:
    args = tool_input(load_input())
    file_path = next(
        (
            value
            for key in ("file_path", "path", "filename")
            if isinstance((value := args.get(key)), str) and value
        ),
        "",
    )

    for pattern in PROTECTED_PATH_PATTERNS:
        if pattern in file_path:
            deny(f"Cannot modify protected file: {file_path} (matched {pattern}).")
            return

    if file_path.endswith(".py"):
        content = "\n".join(
            value for key in ("new_string", "content") if isinstance((value := args.get(key)), str)
        )
        for pattern, reason in DANGEROUS_PYTHON_PATTERNS:
            if pattern in content:
                print(
                    f"WARNING: Potentially dangerous Python pattern `{pattern}` in {file_path}: {reason}",
                    file=sys.stderr,
                )

    command = args.get("command") or args.get("cmd") or ""
    if not isinstance(command, str):
        return
    for pattern, reason in DANGEROUS_SHELL_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            deny(f"Dangerous command blocked: {reason}. Command: {command}")
            return
    for pattern, reason in BLANKET_STAGING_PATTERNS:
        if re.search(pattern, command):
            deny(f"Blanket staging blocked: {reason}. {_BLANKET_STAGING_ADVICE}")
            return
    for pattern, reason in WARNING_SHELL_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            print(
                f"WARNING: Potentially risky operation: {reason}. Command: {command}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
