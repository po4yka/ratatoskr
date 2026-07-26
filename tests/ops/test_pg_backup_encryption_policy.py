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
        "APP_ENV",
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


@pytest.mark.parametrize("app_env", ["production", "staging", "local", "unknown"])
def test_plaintext_override_is_rejected_outside_explicit_dev_test_context(
    tmp_path: Path,
    app_env: str,
) -> None:
    result = _run_script(
        tmp_path,
        APP_ENV=app_env,
        BACKUP_REQUIRE_ENCRYPTION="false",
    )

    assert result.returncode == 1
    assert "allowed only when APP_ENV=development or APP_ENV=test" in result.stdout
    assert "pg_backup_started" not in result.stdout


def test_plaintext_override_defaults_to_production_policy(tmp_path: Path) -> None:
    result = _run_script(tmp_path, BACKUP_REQUIRE_ENCRYPTION="false")

    assert result.returncode == 1
    assert "allowed only when APP_ENV=development or APP_ENV=test" in result.stdout
    assert "pg_backup_started" not in result.stdout


def test_production_rejects_plaintext_policy_even_when_key_is_present(tmp_path: Path) -> None:
    result = _run_script(
        tmp_path,
        APP_ENV="production",
        BACKUP_REQUIRE_ENCRYPTION="false",
        BACKUP_ENCRYPTION_KEY="configured-key",
    )

    assert result.returncode == 1
    assert "allowed only when APP_ENV=development or APP_ENV=test" in result.stdout
    assert "pg_backup_started" not in result.stdout


@pytest.mark.parametrize("app_env", ["development", "test"])
def test_explicit_dev_test_context_reaches_backup_with_plaintext_override(
    tmp_path: Path,
    app_env: str,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pg_dump = bin_dir / "pg_dump"
    pg_dump.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    pg_dump.chmod(0o755)

    result = _run_script(
        tmp_path,
        APP_ENV=app_env,
        BACKUP_REQUIRE_ENCRYPTION="false",
        PATH=f"{bin_dir}:{os.environ['PATH']}",
    )

    assert result.returncode == 1
    assert "pg_backup_started" in result.stdout
    assert "pg_dump failed" in result.stdout
    assert "UNENCRYPTED local backup" in result.stderr
    assert app_env in result.stderr


def test_off_host_copy_cannot_use_plaintext_override(tmp_path: Path) -> None:
    result = _run_script(
        tmp_path,
        APP_ENV="development",
        BACKUP_REQUIRE_ENCRYPTION="false",
        BACKUP_S3_BUCKET="backup-bucket",
    )

    assert result.returncode == 1
    assert "required when BACKUP_S3_BUCKET is set" in result.stdout
