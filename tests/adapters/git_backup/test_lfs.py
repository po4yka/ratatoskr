"""Characterization tests for LfsSupport (port of LfsSupportTest.kt plus argv/detection
coverage via an injected git runner)."""

from __future__ import annotations

from pathlib import Path

from app.adapters.git_backup.git_exec import resolve_git_executable
from app.adapters.git_backup.lfs import LfsSupport

GIT = resolve_git_executable()


class FakeGit:
    def __init__(self, code: int = 0, stdout: str = "") -> None:
        self.code = code
        self.stdout = stdout
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        return self.code, self.stdout


def test_is_lfs_repo_false_for_nonexistent_path(tmp_path: Path) -> None:
    lfs = LfsSupport(run_git=FakeGit())
    assert lfs.is_lfs_repo(tmp_path / "nope") is False


def test_is_lfs_repo_false_for_empty_dir(tmp_path: Path) -> None:
    # No lfs/ dir; git show HEAD:.gitattributes "fails" (not a git repo).
    lfs = LfsSupport(run_git=FakeGit(code=128, stdout=""))
    assert lfs.is_lfs_repo(tmp_path) is False


def test_is_lfs_repo_detects_lfs_directory(tmp_path: Path) -> None:
    (tmp_path / "lfs").mkdir()
    fake = FakeGit()
    lfs = LfsSupport(run_git=fake)
    assert lfs.is_lfs_repo(tmp_path) is True
    assert fake.calls == []  # short-circuits before invoking git


def test_is_lfs_repo_detects_gitattributes_filter(tmp_path: Path) -> None:
    fake = FakeGit(code=0, stdout="*.psd filter=lfs diff=lfs merge=lfs -text\n")
    lfs = LfsSupport(run_git=fake)
    assert lfs.is_lfs_repo(tmp_path) is True
    assert fake.calls == [[GIT, "-C", str(tmp_path), "show", "HEAD:.gitattributes"]]


def test_fetch_lfs_objects_false_for_nonexistent_path(tmp_path: Path) -> None:
    fake = FakeGit()
    lfs = LfsSupport(run_git=fake)
    assert lfs.fetch_lfs_objects(tmp_path / "nope") is False
    assert fake.calls == []


def test_fetch_lfs_objects_argv_and_success(tmp_path: Path) -> None:
    fake = FakeGit(code=0)
    lfs = LfsSupport(run_git=fake)
    assert lfs.fetch_lfs_objects(tmp_path) is True
    assert fake.calls == [[GIT, "-C", str(tmp_path), "lfs", "fetch", "--all"]]


def test_sync_lfs_if_needed_true_for_non_lfs_repo(tmp_path: Path) -> None:
    fake = FakeGit(code=128, stdout="")  # not a git repo / no lfs
    lfs = LfsSupport(run_git=fake)
    assert lfs.sync_lfs_if_needed(tmp_path) is True
    # No fetch attempted for a non-LFS repo.
    assert all("fetch" not in argv for argv in fake.calls)


def test_sync_lfs_if_needed_fetches_for_lfs_repo(tmp_path: Path) -> None:
    (tmp_path / "lfs").mkdir()
    fake = FakeGit(code=0)
    lfs = LfsSupport(run_git=fake)
    assert lfs.sync_lfs_if_needed(tmp_path) is True
    assert fake.calls == [[GIT, "-C", str(tmp_path), "lfs", "fetch", "--all"]]


def test_is_lfs_available(tmp_path: Path) -> None:
    assert LfsSupport(run_git=FakeGit(code=0, stdout="git-lfs/3.5.1")).is_lfs_available() is True
    assert LfsSupport(run_git=FakeGit(code=127)).is_lfs_available() is False
