"""Targeted hermetic tests for uncovered branches in app.adapters.git_backup.lfs.

Uncovered lines addressed here:
- lines 27-42: _default_runner subprocess body (OSError / SubprocessError except handler,
  ssl_environment merging, normal subprocess success/failure).
- line 60: is_lfs_available() exception path returning False.
- line 72: is_lfs_repo() gitattributes show exception path returning False.
- line 80: fetch_lfs_objects() exception path returning False.

All tests are hermetic: no Postgres, no Qdrant, no network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.adapters.git_backup.lfs import LfsSupport, _default_runner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RaisingGit:
    """Fake GitRunner that raises a given exception on every call."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        raise self._exc


# ---------------------------------------------------------------------------
# _default_runner — subprocess body and exception handling
# ---------------------------------------------------------------------------


def test_default_runner_returns_exit_code_and_stdout(tmp_path: Path) -> None:
    """Happy path: a successful subprocess returns its exit code and stdout."""
    runner = _default_runner(ssl_environment={}, timeout_seconds=10.0)
    # Use a simple command that exits 0 and produces deterministic stdout.
    code, out = runner(["python3", "-c", "print('hello')"])
    assert code == 0
    assert "hello" in out


def test_default_runner_nonzero_exit_code(tmp_path: Path) -> None:
    """Non-zero exit from subprocess is forwarded directly (check=False)."""
    runner = _default_runner(ssl_environment={}, timeout_seconds=10.0)
    code, _out = runner(["python3", "-c", "raise SystemExit(42)"])
    assert code == 42


def test_default_runner_oserror_returns_one_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """OSError inside subprocess.run is caught; runner returns (1, '')."""
    runner = _default_runner(ssl_environment={}, timeout_seconds=10.0)

    def _raise_oserror(*args: object, **kwargs: object) -> None:
        raise OSError("no such file")

    monkeypatch.setattr(subprocess, "run", _raise_oserror)
    code, out = runner(["nonexistent-binary"])
    assert code == 1
    assert out == ""


def test_default_runner_subprocess_error_returns_one_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.SubprocessError (e.g. TimeoutExpired) is caught; runner returns (1, '')."""
    runner = _default_runner(ssl_environment={}, timeout_seconds=0.001)

    def _raise_timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=0.001)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    code, out = runner(["git", "lfs", "version"])
    assert code == 1
    assert out == ""


def test_default_runner_merges_ssl_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ssl_environment is non-empty, os.environ is merged with it."""
    captured: dict[str, object] = {}

    def _capture(*args: object, **kwargs: object) -> MagicMock:
        captured["env"] = kwargs.get("env")
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        return m

    monkeypatch.setattr(subprocess, "run", _capture)
    ssl_env = {"SSL_CERT_FILE": "/etc/ssl/certs/ca-bundle.crt"}
    runner = _default_runner(ssl_environment=ssl_env, timeout_seconds=10.0)
    runner(["git", "lfs", "version"])

    env_used = captured["env"]
    assert env_used is not None
    assert env_used["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-bundle.crt"
    # Host env vars should also be present.
    assert "PATH" in env_used or len(env_used) > 1


def test_default_runner_no_ssl_environment_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ssl_environment is empty, env=None is passed to subprocess.run."""
    captured: dict[str, object] = {}

    def _capture(*args: object, **kwargs: object) -> MagicMock:
        captured["env"] = kwargs.get("env")
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        return m

    monkeypatch.setattr(subprocess, "run", _capture)
    runner = _default_runner(ssl_environment={}, timeout_seconds=10.0)
    runner(["git", "lfs", "version"])
    assert captured["env"] is None


# ---------------------------------------------------------------------------
# is_lfs_available() — exception path (line 60)
# ---------------------------------------------------------------------------


def test_is_lfs_available_exception_returns_false() -> None:
    """Any exception raised by run_git is suppressed; returns False."""
    raiser = _RaisingGit(RuntimeError("git not found"))
    lfs = LfsSupport(run_git=raiser)
    assert lfs.is_lfs_available() is False
    assert len(raiser.calls) == 1


def test_is_lfs_available_oserror_returns_false() -> None:
    """OSError from the runner (e.g. git not on PATH) is suppressed."""
    raiser = _RaisingGit(OSError("no such file or directory"))
    lfs = LfsSupport(run_git=raiser)
    assert lfs.is_lfs_available() is False


def test_is_lfs_available_returns_true_on_success() -> None:
    """Sanity check: exit code 0 -> True (existing test also covers this)."""

    class _OkGit:
        def __call__(self, argv: list[str]) -> tuple[int, str]:
            return 0, "git-lfs/3.6.0"

    lfs = LfsSupport(run_git=_OkGit())
    assert lfs.is_lfs_available() is True


# ---------------------------------------------------------------------------
# is_lfs_repo() — gitattributes show exception path (line 72)
# ---------------------------------------------------------------------------


def test_is_lfs_repo_show_exception_returns_false(tmp_path: Path) -> None:
    """Exception from 'git show HEAD:.gitattributes' is suppressed; returns False."""
    raiser = _RaisingGit(subprocess.SubprocessError("git exploded"))
    lfs = LfsSupport(run_git=raiser)
    # tmp_path exists (is_dir() == True) but has no lfs/ subdir.
    assert lfs.is_lfs_repo(tmp_path) is False
    assert len(raiser.calls) == 1


def test_is_lfs_repo_show_oserror_returns_false(tmp_path: Path) -> None:
    """OSError from 'git show' is suppressed; returns False."""
    raiser = _RaisingGit(OSError("exec failed"))
    lfs = LfsSupport(run_git=raiser)
    assert lfs.is_lfs_repo(tmp_path) is False


def test_is_lfs_repo_gitattributes_without_filter_lfs(tmp_path: Path) -> None:
    """'git show' succeeds but stdout lacks 'filter=lfs'; returns False."""

    class _NoLfsAttr:
        def __call__(self, argv: list[str]) -> tuple[int, str]:
            return 0, "*.png binary\n"

    lfs = LfsSupport(run_git=_NoLfsAttr())
    assert lfs.is_lfs_repo(tmp_path) is False


# ---------------------------------------------------------------------------
# fetch_lfs_objects() — exception path (line 80)
# ---------------------------------------------------------------------------


def test_fetch_lfs_objects_exception_returns_false(tmp_path: Path) -> None:
    """Exception from 'git lfs fetch --all' is suppressed; returns False."""
    raiser = _RaisingGit(RuntimeError("network error"))
    lfs = LfsSupport(run_git=raiser)
    assert lfs.fetch_lfs_objects(tmp_path) is False
    assert len(raiser.calls) == 1


def test_fetch_lfs_objects_oserror_returns_false(tmp_path: Path) -> None:
    """OSError from 'git lfs fetch' is suppressed; returns False."""
    raiser = _RaisingGit(OSError("exec error"))
    lfs = LfsSupport(run_git=raiser)
    assert lfs.fetch_lfs_objects(tmp_path) is False


def test_fetch_lfs_objects_nonzero_exit_returns_false(tmp_path: Path) -> None:
    """Non-zero exit from 'git lfs fetch --all' returns False (not an exception)."""

    class _FailFetch:
        def __call__(self, argv: list[str]) -> tuple[int, str]:
            return 1, ""

    lfs = LfsSupport(run_git=_FailFetch())
    assert lfs.fetch_lfs_objects(tmp_path) is False


# ---------------------------------------------------------------------------
# sync_lfs_if_needed() — integration across is_lfs_repo + fetch_lfs_objects
# ---------------------------------------------------------------------------


def test_sync_lfs_if_needed_lfs_dir_fetch_exception_returns_false(tmp_path: Path) -> None:
    """is_lfs_repo() returns True via lfs/ dir; fetch then raises -> False."""
    (tmp_path / "lfs").mkdir()

    call_count = 0

    class _LfsDetectedThenFails:
        def __call__(self, argv: list[str]) -> tuple[int, str]:
            nonlocal call_count
            call_count += 1
            if "fetch" in argv:
                raise OSError("fetch failed")
            return 0, ""

    lfs = LfsSupport(run_git=_LfsDetectedThenFails())
    result = lfs.sync_lfs_if_needed(tmp_path)
    assert result is False
