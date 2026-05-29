"""Git LFS detection and object fetching (port of LfsSupport.kt).

``git clone --mirror`` stores only LFS pointer files, so LFS-enabled repos need a
follow-up ``git lfs fetch --all`` to back up the real content. Detection checks for a
bare-repo ``lfs/`` directory or a ``filter=lfs`` entry in ``HEAD:.gitattributes``.
Commands use the resolved git path; the runner is injectable for hermetic tests.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from app.adapters.git_backup.git_exec import resolve_git_executable

# argv -> (exit_code, stdout)
GitRunner = Callable[[list[str]], tuple[int, str]]


def _default_runner(ssl_environment: Mapping[str, str], timeout_seconds: float) -> GitRunner:
    def run(argv: list[str]) -> tuple[int, str]:
        env = {**os.environ, **ssl_environment} if ssl_environment else None
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return 1, ""
        return proc.returncode, proc.stdout

    return run


class LfsSupport:
    def __init__(
        self,
        *,
        ssl_environment: Mapping[str, str] | None = None,
        timeout_seconds: float = 600.0,
        run_git: GitRunner | None = None,
    ) -> None:
        ssl_environment = ssl_environment or {}
        self._git = resolve_git_executable()
        self._run_git = run_git or _default_runner(ssl_environment, timeout_seconds)

    def is_lfs_available(self) -> bool:
        with contextlib.suppress(Exception):
            return self._run_git([self._git, "lfs", "version"])[0] == 0
        return False

    def is_lfs_repo(self, repo_path: Path) -> bool:
        if not repo_path.is_dir():
            return False
        if (repo_path / "lfs").is_dir():
            return True
        with contextlib.suppress(Exception):
            code, stdout = self._run_git(
                [self._git, "-C", str(repo_path), "show", "HEAD:.gitattributes"]
            )
            return code == 0 and "filter=lfs" in stdout
        return False

    def fetch_lfs_objects(self, repo_path: Path) -> bool:
        if not repo_path.is_dir():
            return False
        with contextlib.suppress(Exception):
            code, _ = self._run_git([self._git, "-C", str(repo_path), "lfs", "fetch", "--all"])
            return code == 0
        return False

    def sync_lfs_if_needed(self, repo_path: Path) -> bool:
        if not self.is_lfs_repo(repo_path):
            return True  # not an LFS repo, nothing to do
        return self.fetch_lfs_objects(repo_path)
