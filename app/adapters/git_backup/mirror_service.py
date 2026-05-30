"""GitMirrorService: orchestrates git mirror sync using the ported gitout engine.

Adapts Engine from gitout to Ratatoskr's async infrastructure:
- Reads mirror targets from GitMirrorRepository (Postgres) instead of TOML config.
- Resolves GitHub credentials from UserGitHubIntegration + Fernet decryption.
- Runs a configurable asyncio.Semaphore worker pool (separate pool for large repos).
- Reports outcomes back to GitMirrorRepository.

Credential handling for GitHub mirrors:
    A short-lived git-credential-store file is written to a tempfile, injected into
    the clone URL is NOT used (to avoid putting tokens in argv and process listings).
    Instead the token is embedded in the https URL via the standard
    https://x-access-token:<token>@github.com/... form and passed as the URL
    argument to git.  The raw token is NEVER logged; only a redacted placeholder
    is logged.  The URL is discarded immediately after the git call.

For manual/arbitrary mirrors the clone URL is used unauthenticated.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse, urlunparse

from app.adapters.git_backup.circuit_breaker import StorageCircuitBreaker
from app.adapters.git_backup.errors import (
    ErrorCategory,
    classify,
    display_name,
    is_permanently_gone,
    should_use_http1_fallback,
)
from app.adapters.git_backup.git_commands import build_git_command
from app.adapters.git_backup.git_exec import resolve_git_executable
from app.adapters.git_backup.lfs import LfsSupport
from app.adapters.git_backup.maintenance import Maintenance, RepositoryMaintenance
from app.adapters.git_backup.retry import RetryContext, RetryPolicy, SyncFailureException
from app.core.git_url_safety import (
    assert_resolved_public_host,
    extract_git_host,
    is_github_host,
)
from app.db.models.git_backup import GitMirror, GitMirrorSource
from app.security.secret_crypto import decrypt_secret

if TYPE_CHECKING:
    from app.adapters.git_backup.repository import GitMirrorRepository
    from app.config.git_backup import GitBackupConfig
    from app.db.session import Database

logger = logging.getLogger(__name__)

# Type alias: (argv, cwd, timeout_seconds) -> (exit_code, combined_output)
GitRunner = Callable[[list[str], Path, float], Awaitable[tuple[int, str]]]

# Regex to strip embedded credentials from any URL-shaped substring for safe
# logging. Matches "<scheme>://<userinfo>@" for any scheme (https, http, git,
# ssh, ...) and collapses the userinfo to "***". Applied to free-form text
# (git stderr, exception messages) so an injected x-access-token never lands in
# a log line or a persisted error.
_CREDENTIAL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)([^/@\s]+@)", re.IGNORECASE)


def _redact_url(text: str) -> str:
    """Replace any 'scheme://user:token@' segment with 'scheme://***@'.

    Operates on arbitrary text and scrubs every match, so it is safe to pass git
    output or exception strings that may embed authenticated clone URLs.
    """
    return _CREDENTIAL_RE.sub(r"\1***@", text)


def _inject_token_into_url(clone_url: str, token: str) -> str:
    """Return a URL with x-access-token:<token> credentials embedded.

    Works for https://github.com/<owner>/<repo>.git URLs.  The token is
    percent-encoded so special characters do not break URL parsing.
    """
    parsed = urlparse(clone_url)
    encoded_token = quote(token, safe="")
    netloc_with_creds = f"x-access-token:{encoded_token}@{parsed.hostname}"
    if parsed.port:
        netloc_with_creds = f"{netloc_with_creds}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc_with_creds))


async def _default_git_runner(
    argv: list[str], cwd: Path, timeout_seconds: float
) -> tuple[int, str]:
    """Run git via asyncio subprocess, capturing combined stdout+stderr."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"git operation timed out after {timeout_seconds}s") from None
    return process.returncode or 0, stdout.decode(errors="replace")


@dataclass(frozen=True)
class MirrorTask:
    """Internal work item for a single mirror operation."""

    mirror: GitMirror
    # Effective clone URL (may have credentials injected for GitHub sources).
    effective_url: str
    # Human-readable name for logs.
    name: str
    destination: Path
    is_large_repo: bool = False


@dataclass(frozen=True)
class MirrorOutcome:
    """Result of a single mirror attempt."""

    mirror: GitMirror
    ok: bool
    skipped: bool = False
    excluded: bool = False
    error: str | None = None
    error_category: ErrorCategory | None = None
    skip_reason: str | None = None
    attempts: int = 1
    clone_strategy: str | None = None


@dataclass
class SyncSummary:
    """Aggregate outcome returned by GitMirrorService.perform_sync."""

    ok: int = 0
    failed: int = 0
    skipped: int = 0
    outcomes: list[MirrorOutcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.ok + self.failed + self.skipped


async def _preflight_storage_check(root: Path, timeout_ms: int) -> str | None:
    """Write/read/delete a sentinel on the backup volume.

    Returns None on success or an error message string on failure.
    """

    def _sync_check(r: Path) -> None:
        if not r.exists():
            raise ValueError(f"Backup destination does not exist: {r}")
        if not r.is_dir():
            raise ValueError(f"Backup destination is not a directory: {r}")
        sentinel = r / f".ratatoskr-preflight-{os.getpid()}-{time.perf_counter_ns()}"
        payload = f"ratatoskr-preflight:{os.getpid()}:{time.perf_counter_ns()}"
        try:
            sentinel.write_text(payload)
            read_back = sentinel.read_text()
            if read_back != payload:
                raise ValueError(
                    f"Preflight sentinel content mismatch: expected {payload!r}, got {read_back!r}"
                )
        finally:
            with contextlib.suppress(FileNotFoundError):
                sentinel.unlink()

    try:
        await asyncio.wait_for(asyncio.to_thread(_sync_check, root), timeout=timeout_ms / 1000)
    except TimeoutError:
        return f"Preflight storage check timed out after {timeout_ms}ms"
    except Exception as exc:
        return str(exc)
    return None


def _should_use_shallow_clone(mirror: GitMirror, cfg: GitBackupConfig) -> bool:
    """Return True when a shallow clone (--depth=1) should be used for this mirror.

    Ports gitout's FailureTracker.get_recommended_strategy logic: shallow clone is
    selected when BOTH the consecutive-failures threshold is met AND the repo size
    exceeds the size threshold. Either threshold set to 0 disables that condition
    (0 = disabled sentinel, not a valid gitout value).

    Only meaningful for initial clones; callers must gate on is_clone.
    """
    failure_threshold = cfg.shallow_clone_after_failures
    size_threshold = cfg.shallow_clone_threshold_kb

    # Both features disabled (default): never shallow.
    if failure_threshold == 0 and size_threshold == 0:
        return False

    # Failures condition: 0 = disabled (skip check), else must be met.
    failures_ok = failure_threshold == 0 or (mirror.consecutive_failures or 0) >= failure_threshold

    # Size condition: 0 = disabled (skip check), else must be met.
    size_ok = size_threshold == 0 or (
        mirror.size_kb is not None and mirror.size_kb >= size_threshold
    )

    # When only one condition is configured, that condition alone governs.
    # When both are configured, both must be met (gitout AND semantics).
    if failure_threshold > 0 and size_threshold > 0:
        return failures_ok and size_ok
    return failures_ok and size_ok


class GitMirrorService:
    """Orchestrates git mirror sync.

    Constructor is fully injectable so tests can supply fakes for every collaborator.
    Production callers build an instance from GitBackupConfig + GitMirrorRepository;
    all collaborators default-construct from config when not provided.
    """

    def __init__(
        self,
        config: GitBackupConfig,
        mirror_repo: GitMirrorRepository,
        db: Database,
        *,
        retry_policy: RetryPolicy | None = None,
        circuit_breaker: StorageCircuitBreaker | None = None,
        maintenance: RepositoryMaintenance | None = None,
        lfs: LfsSupport | None = None,
        git_runner: GitRunner | None = None,
    ) -> None:
        self._config = config
        self._mirror_repo = mirror_repo
        self._db = db
        self._retry_policy = retry_policy or RetryPolicy()
        self._circuit_breaker = circuit_breaker
        # Build maintenance from config if not injected.
        self._maintenance = maintenance or self._build_maintenance()
        # Build LFS support if not injected and fetch_lfs is enabled.
        self._lfs = lfs or self._build_lfs()
        self._git_runner: GitRunner = git_runner or _default_git_runner

    # ------------------------------------------------------------------
    # Collaborator construction from config
    # ------------------------------------------------------------------

    def _build_maintenance(self) -> RepositoryMaintenance | None:
        cfg = self._config
        if cfg.maintenance_strategy == "none":
            return None
        maint_cfg = Maintenance(
            enabled=True,
            strategy=cfg.maintenance_strategy,
            full_repack_interval=cfg.full_repack_interval,
            repack_window=cfg.repack_window,
            repack_depth=cfg.repack_depth,
            write_commit_graph=cfg.write_commit_graph,
        )
        return RepositoryMaintenance(
            maint_cfg,
            timeout_seconds=float(cfg.repository_timeout_seconds),
        )

    def _build_lfs(self) -> LfsSupport | None:
        if not self._config.fetch_lfs:
            return None
        candidate = LfsSupport(timeout_seconds=float(self._config.repository_timeout_seconds))
        return candidate if candidate.is_lfs_available() else None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def perform_sync(
        self,
        user_id: int | None = None,
        *,
        dry_run: bool = False,
    ) -> SyncSummary:
        """Run a full mirror sync cycle.

        Steps:
        1. Preflight storage check (unless dry_run).
        2. Collect mirror tasks from the DB + extra_repos config.
        3. Run tasks in parallel under Semaphore(workers).
        4. Persist outcomes.
        """
        cfg = self._config
        data_path = Path(cfg.data_path)

        if not dry_run:
            preflight_ms = int(cfg.preflight_timeout_seconds * 1000)
            error_msg = await _preflight_storage_check(data_path, timeout_ms=preflight_ms)
            if error_msg is not None:
                logger.error("git_mirror_preflight_failed: %s", error_msg)
                raise RuntimeError(f"Storage pre-flight check failed: {error_msg}")

        # Collect tasks: DB mirrors + static extra_repos
        tasks = await self._collect_tasks(user_id, data_path)

        if dry_run:
            # Emit one plan line per task so operators can verify what would run.
            for task in tasks:
                is_clone = not task.destination.exists()
                plan_argv = build_git_command(
                    repo_exists=not is_clone,
                    url=task.effective_url if is_clone else None,
                    repo_name=task.destination.name if is_clone else None,
                    git_executable=resolve_git_executable(),
                    verify_certificates=cfg.verify_certificates,
                    ssl_ca_info=cfg.ssl_ca_info,
                    http_version=cfg.http_version,
                    post_buffer_size=cfg.post_buffer_size,
                    low_speed_limit=cfg.low_speed_limit,
                    low_speed_time=cfg.low_speed_time,
                    single_branch_only=cfg.single_branch_only,
                    force_http1=bool(task.mirror.use_http1_fallback),
                    use_shallow_clone=_should_use_shallow_clone(task.mirror, cfg) if is_clone else False,
                    show_progress=False,
                    disable_redirects=True,
                )
                # Redact credentials before logging — effective_url may contain
                # an injected x-access-token for GitHub mirrors.
                redacted_argv = [_redact_url(tok) for tok in plan_argv]
                logger.info(
                    "git_mirror_dry_run_plan name=%s dest=%s argv=%s",
                    task.name,
                    task.destination,
                    redacted_argv,
                )
            logger.info(
                "git_mirror_dry_run: %d tasks would run",
                len(tasks),
            )
            return SyncSummary(
                ok=len(tasks),
                outcomes=[MirrorOutcome(mirror=t.mirror, ok=True) for t in tasks],
            )

        # Build a circuit breaker if not injected; use the configured threshold.
        breaker = self._circuit_breaker or StorageCircuitBreaker(
            threshold=cfg.circuit_breaker_threshold
        )

        semaphore = asyncio.Semaphore(cfg.workers)
        large_semaphore = asyncio.Semaphore(cfg.large_repo_max_parallel)

        logger.info("git_mirror_sync_start: tasks=%d workers=%d", len(tasks), cfg.workers)

        async def run_one(task: MirrorTask) -> MirrorOutcome:
            # Fast-path skip if breaker already open.
            if breaker.is_open():
                logger.warning("git_mirror_skip circuit_breaker_open name=%s", task.name)
                return MirrorOutcome(
                    mirror=task.mirror,
                    ok=False,
                    skipped=True,
                    skip_reason="storage circuit breaker open",
                )
            async with semaphore:
                return await self._sync_one(task, breaker, large_semaphore)

        outcomes = list(await asyncio.gather(*(run_one(t) for t in tasks)))

        # Persist outcomes.
        for outcome in outcomes:
            await self._persist_outcome(outcome, tasks)

        summary = SyncSummary(outcomes=outcomes)
        for o in outcomes:
            if o.skipped or o.excluded:
                summary.skipped += 1
            elif o.ok:
                summary.ok += 1
            else:
                summary.failed += 1

        logger.info(
            "git_mirror_sync_done: ok=%d failed=%d skipped=%d",
            summary.ok,
            summary.failed,
            summary.skipped,
        )
        return summary

    # ------------------------------------------------------------------
    # Task collection
    # ------------------------------------------------------------------

    async def _collect_tasks(
        self,
        user_id: int | None,
        data_path: Path,
    ) -> list[MirrorTask]:
        """Build the list of work items from the DB and extra_repos config."""
        cfg = self._config
        threshold_kb = cfg.large_repo_threshold_kb

        # DB-backed mirrors
        due = await self._mirror_repo.list_due(user_id=user_id)
        tasks: list[MirrorTask] = []

        for mirror in due:
            effective_url = await self._resolve_url(mirror)
            dest = self._mirror_destination(data_path, mirror)
            is_large = bool(mirror.size_kb and mirror.size_kb >= threshold_kb)
            tasks.append(
                MirrorTask(
                    mirror=mirror,
                    effective_url=effective_url,
                    name=mirror.name or mirror.clone_url,
                    destination=dest,
                    is_large_repo=is_large,
                )
            )

        # Static extra_repos (not in DB; we create ephemeral GitMirror-like stubs
        # as MANUAL mirrors, upserting them so they get a real DB row and outcomes
        # are persisted).
        for name, url in cfg.extra_repos.items():
            # If we already have this URL from the DB list, skip.
            if any(t.mirror.clone_url == url for t in tasks):
                continue
            # Upsert to ensure a row exists.
            if user_id is not None:
                mirror = await self._mirror_repo.upsert_target(
                    user_id=user_id,
                    source=GitMirrorSource.MANUAL,
                    clone_url=url,
                    name=name,
                )
                dest = self._mirror_destination(data_path, mirror)
                is_large = bool(mirror.size_kb and mirror.size_kb >= threshold_kb)
                tasks.append(
                    MirrorTask(
                        mirror=mirror,
                        effective_url=url,
                        name=name,
                        destination=dest,
                        is_large_repo=is_large,
                    )
                )
            else:
                # No user_id: no DB row possible; run without persistence.
                # Create a minimal synthetic GitMirror so MirrorTask has a mirror field.
                synthetic = GitMirror(
                    id=-1,
                    user_id=0,
                    source=GitMirrorSource.MANUAL,
                    clone_url=url,
                    name=name,
                    consecutive_failures=0,
                )
                dest = data_path / "extra" / name
                tasks.append(
                    MirrorTask(
                        mirror=synthetic,
                        effective_url=url,
                        name=name,
                        destination=dest,
                    )
                )

        return tasks

    def _mirror_destination(self, data_path: Path, mirror: GitMirror) -> Path:
        """Derive the local bare-clone path from the mirror row.

        For GITHUB-source mirrors the on-disk directory is derived from the
        clone URL host so that github.com repos and gist.github.com gists
        (and any future GitHub-owned host) never share a namespace:

        - ``https://github.com/<owner>/<repo>.git``
          → ``<data_path>/github/github.com/<owner>_<repo>.git``
        - ``https://gist.github.com/<id>.git``
          → ``<data_path>/github/gist.github.com/<id>.git``

        Manual mirrors continue to land in ``<data_path>/manual/``.

        Once ``mirror_path`` is populated (after the first successful sync)
        the stored path is returned directly, so the directory is stable
        across service restarts regardless of this derivation logic.
        """
        if mirror.mirror_path:
            return Path(mirror.mirror_path)

        safe_name = (mirror.name or str(mirror.id)).replace("/", "_").replace("..", "_")

        if mirror.source == GitMirrorSource.GITHUB:
            # Use the URL host as a sub-directory to prevent collisions
            # between github.com repos and gist.github.com gists.
            host = extract_git_host(mirror.clone_url) or "github.com"
            return data_path / "github" / host / f"{safe_name}.git"

        return data_path / "manual" / f"{safe_name}.git"

    async def _resolve_url(self, mirror: GitMirror) -> str:
        """Return the effective clone URL, injecting credentials for GitHub mirrors."""
        if mirror.source != GitMirrorSource.GITHUB:
            return mirror.clone_url

        # Authoritative guard: only embed the GitHub token when the URL's real
        # parsed host is exactly github.com. Defends against a mirror row whose
        # clone_url uses a userinfo (github.com@evil.com) or lookalike
        # (github.com.evil.com) host that was misclassified as GITHUB, which
        # would otherwise exfiltrate the user's token to the attacker host.
        if not is_github_host(mirror.clone_url):
            logger.warning(
                "git_mirror_token_skipped_non_github_host user_id=%d mirror_id=%d",
                mirror.user_id,
                mirror.id,
            )
            return mirror.clone_url

        # Look up UserGitHubIntegration for this user.
        from sqlalchemy import select as sa_select

        from app.db.models.repository import UserGitHubIntegration

        async with self._db.session() as session:
            integration = await session.scalar(
                sa_select(UserGitHubIntegration).where(
                    UserGitHubIntegration.user_id == mirror.user_id
                )
            )

        if integration is None or not integration.encrypted_token:
            logger.warning(
                "git_mirror_no_credentials user_id=%d mirror_id=%d",
                mirror.user_id,
                mirror.id,
            )
            return mirror.clone_url  # attempt unauthenticated

        try:
            token = decrypt_secret(integration.encrypted_token)
        except Exception:
            logger.warning(
                "git_mirror_decrypt_failed user_id=%d mirror_id=%d",
                mirror.user_id,
                mirror.id,
            )
            return mirror.clone_url

        return _inject_token_into_url(mirror.clone_url, token)

    # ------------------------------------------------------------------
    # Single-repo sync
    # ------------------------------------------------------------------

    async def _sync_one(
        self,
        task: MirrorTask,
        breaker: StorageCircuitBreaker,
        large_semaphore: asyncio.Semaphore,
    ) -> MirrorOutcome:
        cfg = self._config
        dest = task.destination
        is_clone = not dest.exists()

        # Choose the working directory: parent dir for clone, dest itself for update.
        cwd = dest.parent if is_clone else dest
        if is_clone:
            cwd.mkdir(parents=True, exist_ok=True)

        # Timeout: large repos get a multiplier.
        base_timeout = float(cfg.repository_timeout_seconds)
        timeout = (
            base_timeout * cfg.large_repo_timeout_multiplier if task.is_large_repo else base_timeout
        )

        safe_url = _redact_url(task.effective_url)
        logger.debug(
            "git_mirror_%s name=%s url=%s",
            "clone" if is_clone else "update",
            task.name,
            safe_url,
        )

        # SSRF guard (authoritative, clone time): resolve the target host and
        # refuse to clone from private / loopback / link-local / reserved
        # addresses. Enforced here -- not only at registration -- so DB-sourced
        # rows and config extra_repos are covered and the DNS-rebinding window
        # is narrowed. Blocking DNS is offloaded to a thread.
        host = extract_git_host(task.effective_url)
        if host is None:
            return MirrorOutcome(
                mirror=task.mirror,
                ok=False,
                error="clone URL has no resolvable host",
                error_category=classify("could not resolve host"),
                attempts=0,
            )
        try:
            await asyncio.to_thread(assert_resolved_public_host, host)
        except ValueError as exc:
            logger.warning("git_mirror_blocked name=%s reason=%s", task.name, exc)
            return MirrorOutcome(
                mirror=task.mirror,
                ok=False,
                error=str(exc),
                error_category=classify(str(exc)),
                attempts=0,
            )

        use_shallow = _should_use_shallow_clone(task.mirror, cfg) if is_clone else False
        clone_strategy = "shallow" if use_shallow else "full"

        # Seed HTTP/1.1 from the persisted flag so that a mirror which hit
        # HTTP/2 errors in a prior run starts its first attempt with the
        # fallback already active.  The retry engine may also escalate to
        # HTTP/1.1 within the run via context.should_use_http1_fallback.
        db_http1_seed = bool(task.mirror.use_http1_fallback)

        async def operation(context: RetryContext) -> str:
            argv = build_git_command(
                repo_exists=not is_clone,
                url=task.effective_url if is_clone else None,
                repo_name=dest.name if is_clone else None,
                git_executable=resolve_git_executable(),
                verify_certificates=cfg.verify_certificates,
                ssl_ca_info=cfg.ssl_ca_info,
                http_version=cfg.http_version,
                post_buffer_size=cfg.post_buffer_size,
                low_speed_limit=cfg.low_speed_limit,
                low_speed_time=cfg.low_speed_time,
                single_branch_only=cfg.single_branch_only,
                force_http1=db_http1_seed or context.should_use_http1_fallback,
                use_shallow_clone=use_shallow,
                show_progress=task.is_large_repo or context.is_retry,
                disable_redirects=True,
            )
            code, output = await self._git_runner(argv, cwd, timeout)
            if code != 0:
                # Redact before raising: git stderr commonly echoes the remote
                # URL, which carries the injected x-access-token credential.
                raise RuntimeError(
                    _redact_url(output) if output else f"git exited with code {code}"
                )
            return output

        async def run_with_retry() -> MirrorOutcome:
            try:
                await self._retry_policy.execute(operation, operation_description=safe_url)
            except SyncFailureException as exc:
                category = exc.error_categories[-1] if exc.error_categories else classify(str(exc))
                cause_msg = _redact_url(str(exc.__cause__) if exc.__cause__ else str(exc))
                if is_permanently_gone(cause_msg):
                    logger.warning(
                        "git_mirror_excluded name=%s attempts=%d error=%s",
                        task.name,
                        exc.attempt_count,
                        cause_msg,
                    )
                    return MirrorOutcome(
                        mirror=task.mirror,
                        ok=False,
                        excluded=True,
                        error=cause_msg,
                        error_category=category,
                        attempts=exc.attempt_count,
                        clone_strategy=clone_strategy if is_clone else None,
                    )
                logger.warning(
                    "git_mirror_failed name=%s attempts=%d category=%s error=%s",
                    task.name,
                    exc.attempt_count,
                    display_name(category),
                    cause_msg,
                )
                breaker.record_failure(category)
                return MirrorOutcome(
                    mirror=task.mirror,
                    ok=False,
                    error=cause_msg,
                    error_category=category,
                    attempts=exc.attempt_count,
                    clone_strategy=clone_strategy if is_clone else None,
                )
            except Exception as exc:
                safe_exc = _redact_url(str(exc))
                category = classify(safe_exc)
                if is_permanently_gone(safe_exc):
                    logger.warning(
                        "git_mirror_excluded name=%s error=%s",
                        task.name,
                        safe_exc,
                    )
                    return MirrorOutcome(
                        mirror=task.mirror,
                        ok=False,
                        excluded=True,
                        error=safe_exc,
                        error_category=category,
                        attempts=1,
                        clone_strategy=clone_strategy if is_clone else None,
                    )
                logger.warning(
                    "git_mirror_failed name=%s category=%s error=%s",
                    task.name,
                    display_name(category),
                    safe_exc,
                )
                breaker.record_failure(category)
                return MirrorOutcome(
                    mirror=task.mirror,
                    ok=False,
                    error=safe_exc,
                    error_category=category,
                    attempts=1,
                    clone_strategy=clone_strategy if is_clone else None,
                )

            # Success path.
            logger.debug("git_mirror_ok name=%s", task.name)
            breaker.record_success()

            # Post-sync maintenance (blocking, offloaded to thread).
            if self._maintenance is not None:
                await asyncio.to_thread(self._maintenance.run_post_sync_maintenance, dest)
                self._maintenance.register_sync_and_check_repack()

            # LFS fetch (blocking, offloaded to thread).
            if self._lfs is not None:
                await asyncio.to_thread(self._lfs.sync_lfs_if_needed, dest)

            return MirrorOutcome(
                mirror=task.mirror,
                ok=True,
                attempts=1,
                clone_strategy=clone_strategy if is_clone else None,
            )

        if task.is_large_repo and is_clone:
            async with large_semaphore:
                return await run_with_retry()
        return await run_with_retry()

    # ------------------------------------------------------------------
    # Outcome persistence
    # ------------------------------------------------------------------

    async def _persist_outcome(
        self,
        outcome: MirrorOutcome,
        tasks: list[MirrorTask],
    ) -> None:
        """Write the outcome back to the DB (skips synthetic rows with id=-1)."""
        mirror = outcome.mirror
        if mirror.id is None or mirror.id < 0:
            return  # synthetic / no DB row

        if outcome.excluded:
            await self._mirror_repo.record_excluded(
                mirror.id,
                outcome.error or "repository permanently gone",
            )
            return

        if outcome.skipped:
            await self._mirror_repo.record_skip(
                mirror.id,
                outcome.skip_reason or "skipped",
            )
            return

        if outcome.ok:
            # Find the matching task to get the destination path and size.
            task = next((t for t in tasks if t.mirror.id == mirror.id), None)
            dest = task.destination if task else None
            size_kb: int | None = None
            if dest and dest.exists():
                try:
                    size_kb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) // 1024
                except OSError:
                    size_kb = None

            await self._mirror_repo.record_success(
                mirror_id=mirror.id,
                mirror_path=str(dest) if dest else "",
                size_kb=size_kb,
                default_branch=mirror.default_branch,
                clone_strategy=outcome.clone_strategy,
            )
        else:
            # Derive whether this failure involved an HTTP/2 error so we can
            # persist the fallback flag for the next run.  Only set it (True);
            # never clear it via record_failure — clearing is record_success's job.
            use_http1: bool | None = None
            if outcome.error_category is not None and should_use_http1_fallback(
                outcome.error_category
            ):
                use_http1 = True
            await self._mirror_repo.record_failure(
                mirror_id=mirror.id,
                error_category=outcome.error_category or ErrorCategory.UNKNOWN,
                message=outcome.error or "",
                clone_strategy=outcome.clone_strategy,
                use_http1=use_http1,
            )


__all__ = [
    "GitMirrorService",
    "MirrorOutcome",
    "MirrorTask",
    "SyncSummary",
]
