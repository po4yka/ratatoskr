from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run_hook(relative_path: str, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / relative_path)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def test_claude_pre_tool_hook_blocks_protected_file() -> None:
    result = _run_hook(
        ".claude/hooks/pre-tool-use.py",
        {"tool_input": {"file_path": "requirements.txt", "content": "changed"}},
    )

    output = json.loads(result.stdout)
    assert result.returncode == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_claude_pre_tool_hook_blocks_dangerous_shell_command() -> None:
    command = "rm " + "-rf /"
    result = _run_hook(
        ".claude/hooks/pre-tool-use.py",
        {"tool_input": {"command": command}},
    )

    output = json.loads(result.stdout)
    assert result.returncode == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_claude_prompt_hook_emits_current_database_and_frontend_context() -> None:
    result = _run_hook(
        ".claude/hooks/user-prompt-submit.py",
        {"prompt": "Debug the database and frontend"},
    )

    assert result.returncode == 0
    assert "PostgreSQL" in result.stdout
    assert "ratatoskr-web" in result.stdout
    assert "ratatoskr.db" not in result.stdout
