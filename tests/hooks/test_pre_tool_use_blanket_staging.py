"""Tests for the PreToolUse hook's blanket-staging guard.

The guard exists because `git add -A` in a checkout worked on by several sessions
stages whatever another session has half-written. That is not a hypothetical:
f6f68b00 carries an entire star-list subsystem under a commit message about Pi
container networks.

A guard like this fails in two directions, and both are silent. Too loose and it
blocks a commit whose *message* merely mentions `--all`; too tight and the thing
it exists to stop walks straight through. The table below is the sample the
patterns were tuned against, kept so a later edit has to keep passing it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "pre-tool-use.py"


def _decision(command: str) -> str:
    """Run the hook exactly as Claude Code does and return its decision."""
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        check=False,
    )
    stdout = result.stdout.strip()
    if not stdout:
        return "allow"
    try:
        return json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]
    except (json.JSONDecodeError, KeyError):
        return "allow"


@pytest.mark.parametrize(
    "command",
    [
        "git add -A",
        "git add --all",
        "git add -A .",
        "git add .",
        "git add :/",
        "git add --all -- .",
        # command position after a separator, which is how it usually shows up
        "cd app && git add -A",
        "git status && git add --all",
        # -C does not make it someone else's index
        "git -C /tmp/repo add -A",
        # Wrapper prefixes. This is the case the unit tests originally missed: the
        # RTK hook rewrites every Bash git call into `rtk git ...` before this hook
        # sees it, so a pattern anchored straight onto `git` passed all its tests
        # while blocking nothing that was actually run. Caught only by executing
        # the real command against the real hook chain.
        "rtk git add -A",
        "rtk git add .",
        "rtk git commit -am x",
        "rtk git -C /tmp/repo add --all",
        "cd app && rtk git add -A",
        "sudo git add .",
        "GIT_AUTHOR_NAME=x git add --all",
        # commit -a stages every tracked modification, same hazard
        "git commit -a",
        'git commit -am "msg"',
        'git commit -a -m "msg"',
        'git commit --all -m "msg"',
    ],
)
def test_blanket_staging_is_blocked(command: str) -> None:
    assert _decision(command) == "deny", command


@pytest.mark.parametrize(
    "command",
    [
        # the whole point: explicit paths are how you are meant to stage
        "git add tests/foo.py",
        "git add app/di/api.py app/di/tasks.py",
        "git add -- path/with/dashes.py",
        "git add docs/explanation/github-repository-ingestion.md",
        'git add "weird -A name.txt"',
        # --amend must not read as "commit everything"
        "git commit --amend --no-edit",
        'git commit --amend -m "reworded"',
        'git commit -m "msg"',
        # a flag named inside a message or a quoted string is text, not a flag
        'git commit -m "explain git add --all in docs"',
        "git commit -m 'prefer -a over nothing'",
        'echo "never use git add -A here"',
        # a wrapper must not turn a legitimate call into a blocked one either
        "rtk git add path/to/file.py",
        "rtk git add -- docs/a.md",
        'rtk git commit -m "explain git add --all"',
        "rtk git log --oneline -5",
        # ordinary read-only git
        "git status --short --branch",
        "git diff --stat",
        "git log --oneline -5",
        "git notes show f6f68b00",
        "git push origin main",
    ],
)
def test_legitimate_commands_are_allowed(command: str) -> None:
    assert _decision(command) != "deny", command


def test_denial_explains_what_to_do_instead() -> None:
    """A guard that only says no teaches nothing; it has to name the alternative."""
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_input": {"command": "git add -A"}}),
        capture_output=True,
        text=True,
        check=False,
    )
    reason = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "git add path/one path/two" in reason
    assert "git status --short" in reason


def test_unrelated_tools_are_untouched() -> None:
    """The hook sees every tool call; a payload with no command must pass."""
    assert _decision("") == "allow"
