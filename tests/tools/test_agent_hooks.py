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


def test_claude_project_settings_deny_env_access() -> None:
    settings = json.loads((ROOT / ".claude/settings.json").read_text())

    permissions = settings["permissions"]
    assert permissions.get("allow", []) == []
    assert set(permissions["deny"]) == {"Read(.env)", "Edit(.env)"}


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


def test_codex_pre_tool_hook_extracts_protected_path_from_apply_patch() -> None:
    result = _run_hook(
        ".codex/hooks/pre-tool-use.py",
        {
            "tool_input": {
                "patch": "*** Begin Patch\n*** Update File: requirements.txt\n@@\n-old\n+new\n*** End Patch"
            }
        },
    )

    output = json.loads(result.stdout)
    assert result.returncode == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_codex_pre_tool_hook_checks_python_content_from_apply_patch() -> None:
    result = _run_hook(
        ".codex/hooks/pre-tool-use.py",
        {
            "tool_input": {
                "patch": "*** Begin Patch\n*** Update File: app/example.py\n@@\n-old\n+eval(value)\n*** End Patch"
            }
        },
    )

    assert result.returncode == 0
    assert "Arbitrary code evaluation" in result.stderr


def test_codex_post_tool_hook_lints_python_path_from_apply_patch() -> None:
    result = _run_hook(
        ".codex/hooks/post-tool-use.py",
        {
            "tool_input": "*** Begin Patch\n*** Update File: tests/tools/test_agent_hooks.py\n*** End Patch"
        },
    )

    assert result.returncode == 0
    assert "Running quick lint on tests/tools/test_agent_hooks.py" in result.stdout


def test_codex_session_hook_uses_project_venv_without_reading_env() -> None:
    session_hook = (ROOT / ".codex/hooks/session-start.sh").read_text()

    assert 'python_command=".venv/bin/python"' in session_hook
    assert "contents not inspected" in session_hook
    assert "OPENROUTER_API_KEY" not in session_hook
    assert 'grep -q "^${key}=" .env' not in session_hook
