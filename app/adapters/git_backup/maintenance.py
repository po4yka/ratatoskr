"""Post-sync repository maintenance (port of RepositoryMaintenance.kt via gitout).

Strategies: ``gc-auto`` (``git gc --auto``), ``geometric`` (``git repack
--geometric=2 -d``), or ``none``. Optionally writes a commit-graph after every sync
and runs a periodic full repack (``git repack -a -d``) on a weekly/monthly cadence
(~1 sync/day -> 7 / 30 syncs). Maintenance commands use the literal ``git`` (matching
Kotlin). The command runner is injectable so tests assert argv without spawning git.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# (argv, cwd) -> None
GitCommandRunner = Callable[[list[str], Path], None]

# Max characters of captured git stderr surfaced in a failure log line (avoids
# flooding logs with a full repack/gc dump while keeping the actionable tail).
_STDERR_LOG_LIMIT = 2000


def _decode_stderr(raw: bytes | str | None) -> str:
    """Decode + tail captured git stderr into a bounded, log-safe string."""
    if raw is None:
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    text = text.strip()
    return text[-_STDERR_LOG_LIMIT:] if len(text) > _STDERR_LOG_LIMIT else text


@dataclass
class Maintenance:
    """Configuration for post-sync repository maintenance.

    Mirrors the ``Maintenance`` dataclass from gitout's config module.
    """

    enabled: bool = False
    strategy: str = "gc-auto"
    full_repack_interval: str = "never"
    repack_window: int = 50
    repack_depth: int = 50
    write_commit_graph: bool = True


def _default_runner(timeout_seconds: float) -> GitCommandRunner:
    def run(argv: list[str], cwd: Path) -> None:
        # Defense-in-depth: restrict git itself to the transports
        # assert_safe_git_url allows (see app/core/git_url_safety.py).
        env = {
            **os.environ,
            "GIT_ALLOW_PROTOCOL": "https:http:git:ssh",
            "GIT_PROTOCOL_FROM_USER": "0",
        }
        # Best-effort: a maintenance failure must NEVER abort the backup sync (callers
        # invoke this via asyncio.to_thread with no try/except), but it MUST be
        # observable. Previously the whole call was swallowed (check=False +
        # contextlib.suppress(Exception)) so a failing gc/repack/commit-graph, or a
        # timeout, vanished with no trace. argv here is fixed maintenance verbs on a
        # local path (no URL / token), so it is safe to log unredacted.
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                env=env,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            # Timeout, spawn failure (git missing / not executable), etc. Logged and
            # swallowed -- the sync continues without this repo's maintenance.
            logger.warning("git_maintenance_command_error argv=%s cwd=%s error=%s", argv, cwd, exc)
            return
        if completed.returncode != 0:
            # check=False means a non-zero exit does NOT raise; surface it explicitly
            # with the git stderr so a corrupt repo / failed repack is diagnosable.
            logger.warning(
                "git_maintenance_command_failed argv=%s cwd=%s rc=%s stderr=%s",
                argv,
                cwd,
                completed.returncode,
                _decode_stderr(completed.stderr),
            )

    return run


class RepositoryMaintenance:
    def __init__(
        self,
        config: Maintenance,
        *,
        timeout_seconds: float = 600.0,
        run_git: GitCommandRunner | None = None,
    ) -> None:
        self._config = config
        self._run_git = run_git or _default_runner(timeout_seconds)
        self._sync_count = 0

    def run_post_sync_maintenance(self, repo_path: Path) -> None:
        if not self._config.enabled:
            return
        if not repo_path.is_dir():
            return

        abs_path = str(repo_path)
        if self._config.strategy == "gc-auto":
            self._run_git(["git", "-C", abs_path, "gc", "--auto"], repo_path)
        elif self._config.strategy == "geometric":
            self._run_git(["git", "-C", abs_path, "repack", "--geometric=2", "-d"], repo_path)
        # "none" and unknown strategies run no repack command (unknown is a no-op here).

        # The commit-graph is written after every strategy, including none/unknown.
        if self._config.write_commit_graph:
            self._run_git(
                ["git", "-C", abs_path, "commit-graph", "write", "--reachable"], repo_path
            )

    def register_sync_and_check_repack(self) -> bool:
        """Record that a sync completed and return whether a periodic full repack is now due."""
        if not self._config.enabled:
            return False
        self._sync_count += 1
        interval = self._config.full_repack_interval
        if interval == "weekly":
            return self._sync_count % 7 == 0
        if interval == "monthly":
            return self._sync_count % 30 == 0
        return False  # "never" or unknown

    def run_full_repack(self, destination_path: Path) -> None:
        if not self._config.enabled:
            return
        if not destination_path.is_dir():
            return
        for repo in self.find_git_repos(destination_path):
            self._run_git(
                [
                    "git",
                    "-C",
                    str(repo),
                    "repack",
                    "-a",
                    "-d",
                    f"--window={self._config.repack_window}",
                    f"--depth={self._config.repack_depth}",
                ],
                repo,
            )

    @staticmethod
    def find_git_repos(root: Path) -> list[Path]:
        """Bare repos (dirs containing a HEAD file) under ``root``, to a depth of 4."""
        if not root.exists():
            return []
        repos: list[Path] = []
        for dirpath, dirnames, _ in os.walk(root):
            current = Path(dirpath)
            depth = len(current.relative_to(root).parts)
            if depth > 4:
                dirnames[:] = []
                continue
            if (current / "HEAD").exists():
                repos.append(current)
        return repos
