"""Resolution of the ``git`` executable (port of GIT_EXECUTABLE in util.kt)."""

from __future__ import annotations

import shutil
from functools import cache


@cache
def resolve_git_executable() -> str:
    """Absolute path to ``git`` via PATH search, falling back to ``git`` if not found."""
    return shutil.which("git") or "git"
