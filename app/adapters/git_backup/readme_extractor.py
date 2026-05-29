"""Extract a README from a bare repository (port of ReadmeExtractor.kt).

Tries README.md, readme.md, README.rst, README.txt, README in order via
``git --git-dir=<path> show HEAD:<name>`` and truncates the content to 8000 chars.
The git runner is injectable; the default shells out to ``git`` (matching Kotlin).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

README_CANDIDATES = ("README.md", "readme.md", "README.rst", "README.txt", "README")
MAX_CHARS = 8000

# (git_dir, ref) -> (exit_code, stdout). Returns nonzero when the file is absent.
GitShowRunner = Callable[[str, str], tuple[int, str]]


def _default_runner(timeout_seconds: float) -> GitShowRunner:
    def run(git_dir: str, ref: str) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                ["git", f"--git-dir={git_dir}", "show", ref],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return 1, ""
        return proc.returncode, proc.stdout

    return run


class ReadmeExtractor:
    def __init__(
        self, *, timeout_seconds: float = 30.0, run_git_show: GitShowRunner | None = None
    ) -> None:
        self._run = run_git_show or _default_runner(timeout_seconds)

    def extract(self, bare_repo_path: Path) -> str:
        """Return the first README found (truncated to 8000 chars), or '' if none."""
        git_dir = str(bare_repo_path.absolute())
        for filename in README_CANDIDATES:
            try:
                code, output = self._run(git_dir, f"HEAD:{filename}")
            except Exception:  # defensive, matches Kotlin's broad catch
                continue
            if code == 0:
                return output[:MAX_CHARS]
        return ""
