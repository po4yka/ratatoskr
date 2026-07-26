"""Characterization tests for RepositoryMaintenance (port of RepositoryMaintenanceTest.kt
plus git-argv coverage via an injected command runner)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from app.adapters.git_backup import maintenance as maintenance_mod
from app.adapters.git_backup.maintenance import (
    Maintenance,
    RepositoryMaintenance,
    _default_runner,
)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv: list[str], cwd: Path) -> None:
        self.calls.append((argv, cwd))

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


def _make(config: Maintenance) -> tuple[RepositoryMaintenance, RecordingRunner]:
    runner = RecordingRunner()
    return RepositoryMaintenance(config, run_git=runner), runner


# --- shouldRunFullRepack counters (ported from RepositoryMaintenanceTest.kt) ---


def test_full_repack_disabled() -> None:
    m, _ = _make(Maintenance(enabled=False))
    assert m.register_sync_and_check_repack() is False


def test_full_repack_never() -> None:
    m, _ = _make(Maintenance(enabled=True, full_repack_interval="never"))
    assert all(m.register_sync_and_check_repack() is False for _ in range(100))


def test_full_repack_weekly_every_7() -> None:
    m, _ = _make(Maintenance(enabled=True, full_repack_interval="weekly"))
    results = [m.register_sync_and_check_repack() for _ in range(14)]
    assert [i + 1 for i, r in enumerate(results) if r] == [7, 14]


def test_full_repack_monthly_every_30() -> None:
    m, _ = _make(Maintenance(enabled=True, full_repack_interval="monthly"))
    results = [m.register_sync_and_check_repack() for _ in range(30)]
    assert [i + 1 for i, r in enumerate(results) if r] == [30]


# --- post-sync maintenance argv ---


def test_disabled_runs_nothing(tmp_path: Path) -> None:
    m, runner = _make(Maintenance(enabled=False))
    m.run_post_sync_maintenance(tmp_path)
    assert runner.calls == []


def test_nonexistent_path_runs_nothing(tmp_path: Path) -> None:
    m, runner = _make(Maintenance(enabled=True, strategy="gc-auto"))
    m.run_post_sync_maintenance(tmp_path / "nope")
    assert runner.calls == []


def test_gc_auto_then_commit_graph(tmp_path: Path) -> None:
    m, runner = _make(Maintenance(enabled=True, strategy="gc-auto", write_commit_graph=True))
    m.run_post_sync_maintenance(tmp_path)
    assert runner.argvs == [
        ["git", "-C", str(tmp_path), "gc", "--auto"],
        ["git", "-C", str(tmp_path), "commit-graph", "write", "--reachable"],
    ]


def test_geometric_strategy(tmp_path: Path) -> None:
    m, runner = _make(Maintenance(enabled=True, strategy="geometric", write_commit_graph=False))
    m.run_post_sync_maintenance(tmp_path)
    assert runner.argvs == [["git", "-C", str(tmp_path), "repack", "--geometric=2", "-d"]]


def test_unknown_strategy_still_writes_commit_graph(tmp_path: Path) -> None:
    m, runner = _make(Maintenance(enabled=True, strategy="bogus", write_commit_graph=True))
    m.run_post_sync_maintenance(tmp_path)
    assert runner.argvs == [
        ["git", "-C", str(tmp_path), "commit-graph", "write", "--reachable"],
    ]


# --- full repack across discovered repos ---


def _make_bare_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "HEAD").write_text("ref: refs/heads/main\n")


def test_run_full_repack_over_discovered_repos(tmp_path: Path) -> None:
    _make_bare_repo(tmp_path / "a")
    _make_bare_repo(tmp_path / "nested" / "b")
    (tmp_path / "not-a-repo").mkdir()
    m, runner = _make(Maintenance(enabled=True, repack_window=42, repack_depth=7))

    m.run_full_repack(tmp_path)

    repacked = {argv[2] for argv in runner.argvs}
    assert repacked == {str(tmp_path / "a"), str(tmp_path / "nested" / "b")}
    for argv in runner.argvs:
        assert argv[3:] == ["repack", "-a", "-d", "--window=42", "--depth=7"]


def test_run_full_repack_nonexistent_path(tmp_path: Path) -> None:
    m, runner = _make(Maintenance(enabled=True))
    m.run_full_repack(tmp_path / "missing")
    assert runner.calls == []


# --- default runner: subprocess failures are observable, never swallowed ---


def test_default_runner_logs_nonzero_exit(monkeypatch, caplog, tmp_path: Path) -> None:
    """A non-zero git exit (check=False, so it does not raise) is logged with the
    return code + stderr instead of vanishing silently."""
    completed = subprocess.CompletedProcess(
        args=["git"], returncode=1, stdout=b"", stderr=b"fatal: gc failed\n"
    )
    monkeypatch.setattr(maintenance_mod.subprocess, "run", MagicMock(return_value=completed))
    run = _default_runner(600.0)

    with caplog.at_level(logging.WARNING):
        run(["git", "-C", str(tmp_path), "gc", "--auto"], tmp_path)

    assert "git_maintenance_command_failed" in caplog.text
    assert "rc=1" in caplog.text
    assert "fatal: gc failed" in caplog.text


def test_default_runner_logs_and_swallows_subprocess_error(
    monkeypatch, caplog, tmp_path: Path
) -> None:
    """A raised subprocess error (e.g. timeout) is logged AND swallowed -- the sync
    must never be aborted by a maintenance failure (callers have no try/except)."""

    def _raise(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=600.0)

    monkeypatch.setattr(maintenance_mod.subprocess, "run", _raise)
    run = _default_runner(600.0)

    with caplog.at_level(logging.WARNING):
        run(["git", "-C", str(tmp_path), "gc", "--auto"], tmp_path)  # must NOT raise

    assert "git_maintenance_command_error" in caplog.text


def test_default_runner_success_logs_nothing(monkeypatch, caplog, tmp_path: Path) -> None:
    """A clean (rc=0) maintenance command emits no warning."""
    completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(maintenance_mod.subprocess, "run", MagicMock(return_value=completed))
    run = _default_runner(600.0)

    with caplog.at_level(logging.WARNING):
        run(["git", "-C", str(tmp_path), "gc", "--auto"], tmp_path)

    assert caplog.records == []
