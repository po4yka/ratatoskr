"""Execution tests for the PostgreSQL backup encryption policy."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops/docker/pg-backup/run-backup.sh"


def _run_script(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "BACKUP_DIR": str(tmp_path / "backups"),
        "BACKUP_METRICS_DIR": str(tmp_path / "metrics"),
        "BACKUP_RETENTION_DAYS": "14",
    }
    for key in (
        "BACKUP_ENCRYPTION_KEY",
        "BACKUP_REQUIRE_ENCRYPTION",
        "BACKUP_S3_BUCKET",
    ):
        env.pop(key, None)
    env.update(overrides)
    return subprocess.run(
        ["sh", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def test_missing_key_fails_closed_by_default(tmp_path: Path) -> None:
    result = _run_script(tmp_path)

    assert result.returncode == 1
    assert "BACKUP_ENCRYPTION_KEY is required" in result.stdout
    assert not list((tmp_path / "backups").glob("ratatoskr-postgres-*"))


@pytest.mark.parametrize("value", ["maybe", "enabled"])
def test_invalid_encryption_policy_is_rejected(tmp_path: Path, value: str) -> None:
    result = _run_script(tmp_path, BACKUP_REQUIRE_ENCRYPTION=value)

    assert result.returncode == 1
    assert "BACKUP_REQUIRE_ENCRYPTION must be a boolean" in result.stdout


def test_off_host_copy_cannot_use_plaintext_override(tmp_path: Path) -> None:
    result = _run_script(
        tmp_path,
        BACKUP_REQUIRE_ENCRYPTION="false",
        BACKUP_S3_BUCKET="backup-bucket",
    )

    assert result.returncode == 1
    assert "required when BACKUP_S3_BUCKET is set" in result.stdout
