from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "check_ci_performance.sh"
REQUIRED_COMMANDS = ("gh", "jq", "column")


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_script(tmp_path: Path, runs: list[dict[str, object]]) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "gh",
        """#!/bin/sh
case " $* " in
  *" --json conclusion,createdAt,headBranch,startedAt,updatedAt "*) ;;
  *) echo "unexpected gh arguments: $*" >&2; exit 64 ;;
esac
printf '%s\n' "$GH_RUNS_FIXTURE"
""",
    )
    _write_executable(fake_bin / "column", "#!/bin/sh\n/bin/cat\n")

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["GH_RUNS_FIXTURE"] = json.dumps(runs)
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_reports_duration_from_supported_timestamp_fields(tmp_path: Path) -> None:
    runs = [
        {
            "conclusion": "success",
            "createdAt": "2026-07-15T12:00:00Z",
            "headBranch": "main",
            "startedAt": "2026-07-15T12:01:00Z",
            "updatedAt": "2026-07-15T12:11:00Z",
        },
        {
            "conclusion": "success",
            "createdAt": "2026-07-14T12:00:00Z",
            "headBranch": "feature/ci",
            "startedAt": "2026-07-14T12:02:00Z",
            "updatedAt": "2026-07-14T12:22:00Z",
        },
        {
            "conclusion": "failure",
            "createdAt": "2026-07-13T12:00:00Z",
            "headBranch": "main",
            "startedAt": "2026-07-13T12:00:00Z",
            "updatedAt": "2026-07-13T13:00:00Z",
        },
    ]

    result = _run_script(tmp_path, runs)

    assert result.returncode == 0, result.stderr
    assert "2026-07-15 | main | 10 min" in result.stdout
    assert "2026-07-14 | feature/ci | 20 min" in result.stdout
    assert "Average CI time (successful runs in this sample): 15 minutes" in result.stdout
    assert "Warm cache: ≤15 min" in result.stdout
    assert "Cold cache: ≤20 min" in result.stdout


def test_handles_empty_run_history(tmp_path: Path) -> None:
    result = _run_script(tmp_path, [])

    assert result.returncode == 0, result.stderr
    assert "No successful runs with complete timing data were found." in result.stdout
    assert "Average CI time (successful runs in this sample): n/a" in result.stdout


@pytest.mark.parametrize("missing_command", REQUIRED_COMMANDS)
def test_reports_missing_required_commands(tmp_path: Path, missing_command: str) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for command in REQUIRED_COMMANDS:
        if command != missing_command:
            _write_executable(fake_bin / command, "#!/bin/sh\nexit 0\n")

    result = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env={"PATH": str(fake_bin)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert f"Missing required command(s): {missing_command}" in result.stdout
