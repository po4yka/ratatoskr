"""Git mirror backup configuration.

Drives the Taskiq git-backup sync job that mirrors GitHub repositories (starred,
owned, watched) and arbitrary extra repos to a local bare-clone store using the
gitout engine. The DB-persisted GitMirror table is the primary source of repos
to mirror; extra_repos provides a lightweight override for repos that do not
need a DB row (e.g. one-off external projects).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GitBackupConfig(BaseModel):
    """Git mirror backup configuration for the gitout-backed sync job."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=False,
        validation_alias="GIT_BACKUP_ENABLED",
        description=(
            "Master switch for the periodic git-backup Taskiq job. "
            "When false the job is not registered with the scheduler."
        ),
    )
    sync_cron: str = Field(
        default="0 4 * * *",
        validation_alias="GIT_BACKUP_SYNC_CRON",
        description="UTC cron expression for the git mirror sync job.",
    )
    data_path: str = Field(
        default="/data/git-mirrors",
        validation_alias="GIT_BACKUP_DATA_PATH",
        description=(
            "Host path (or container-internal path) where bare git clones are stored. "
            "Must be a writable directory; typically bind-mounted from the host."
        ),
    )

    # Parallelism
    workers: int = Field(
        default=4,
        ge=1,
        le=32,
        validation_alias="GIT_BACKUP_WORKERS",
        description="Number of parallel git clone/fetch workers (1–32).",
    )
    repository_timeout_seconds: int = Field(
        default=3600,
        ge=1,
        validation_alias="GIT_BACKUP_REPO_TIMEOUT_SECONDS",
        description="Per-repository operation timeout in seconds.",
    )

    # LFS
    fetch_lfs: bool = Field(
        default=False,
        validation_alias="GIT_BACKUP_FETCH_LFS",
        description="Fetch Git LFS objects during mirror operations.",
    )

    # Maintenance
    maintenance_strategy: str = Field(
        default="gc-auto",
        validation_alias="GIT_BACKUP_MAINTENANCE_STRATEGY",
        description=(
            "Post-fetch maintenance strategy applied to each mirror. "
            "Accepted values: gc-auto, geometric, none."
        ),
    )
    full_repack_interval: str = Field(
        default="never",
        validation_alias="GIT_BACKUP_FULL_REPACK_INTERVAL",
        description=(
            "How often to perform a full repack of each mirror. "
            "Accepted values: never, weekly, monthly."
        ),
    )
    write_commit_graph: bool = Field(
        default=True,
        validation_alias="GIT_BACKUP_WRITE_COMMIT_GRAPH",
        description="Write a commit-graph file after each mirror update for faster graph walks.",
    )

    # Large-repo tuning
    large_repo_threshold_kb: int = Field(
        default=512000,
        ge=1,
        validation_alias="GIT_BACKUP_LARGE_REPO_THRESHOLD_KB",
        description=(
            "Repository disk size in KB above which large-repo handling applies "
            "(extended timeout, reduced parallelism)."
        ),
    )
    large_repo_timeout_multiplier: int = Field(
        default=3,
        ge=1,
        validation_alias="GIT_BACKUP_LARGE_REPO_TIMEOUT_MULTIPLIER",
        description=(
            "Multiplier applied to repository_timeout_seconds for repos that exceed "
            "large_repo_threshold_kb."
        ),
    )
    large_repo_max_parallel: int = Field(
        default=2,
        ge=1,
        validation_alias="GIT_BACKUP_LARGE_REPO_MAX_PARALLEL",
        description="Maximum number of large repos mirrored concurrently.",
    )

    # Failure tracking
    max_consecutive_failures: int = Field(
        default=5,
        ge=1,
        validation_alias="GIT_BACKUP_MAX_CONSECUTIVE_FAILURES",
        description=(
            "Number of consecutive failures before a repo is flagged as failing "
            "and subject to the cooldown policy."
        ),
    )
    failure_cooldown_hours: int = Field(
        default=24,
        ge=0,
        validation_alias="GIT_BACKUP_FAILURE_COOLDOWN_HOURS",
        description=(
            "Hours to wait before retrying a repo that has exceeded "
            "max_consecutive_failures."
        ),
    )
    auto_skip_failing: bool = Field(
        default=True,
        validation_alias="GIT_BACKUP_AUTO_SKIP_FAILING",
        description=(
            "Automatically skip repos that are in the failure-cooldown window "
            "instead of retrying them every run."
        ),
    )

    # Arbitrary extra repos (supplement the DB GitMirror table)
    extra_repos: dict[str, str] = Field(
        default_factory=dict,
        validation_alias="GIT_BACKUP_EXTRA_REPOS",
        description=(
            "Mapping of short name -> clone URL for repositories that should be "
            "mirrored but do not have a GitMirror DB row. "
            "Example: {'my-project': 'https://github.com/user/my-project.git'}. "
            "Parsing a nested dict from a flat env var is awkward; prefer the DB "
            "GitMirror table for dynamic configuration and reserve this field for "
            "static, deployment-time overrides supplied via ratatoskr.yaml."
        ),
    )

    @field_validator("sync_cron", mode="before")
    @classmethod
    def _validate_sync_cron(cls, value: Any) -> str:
        if value in (None, ""):
            return "0 4 * * *"
        cron = str(value).strip()
        if len(cron.split()) != 5:
            msg = "GIT_BACKUP_SYNC_CRON must be a 5-field cron expression"
            raise ValueError(msg)
        return cron

    @field_validator("maintenance_strategy", mode="before")
    @classmethod
    def _validate_maintenance_strategy(cls, value: Any) -> str:
        if value in (None, ""):
            return "gc-auto"
        strategy = str(value).strip()
        allowed = {"gc-auto", "geometric", "none"}
        if strategy not in allowed:
            msg = f"GIT_BACKUP_MAINTENANCE_STRATEGY must be one of {sorted(allowed)}, got {strategy!r}"
            raise ValueError(msg)
        return strategy

    @field_validator("full_repack_interval", mode="before")
    @classmethod
    def _validate_full_repack_interval(cls, value: Any) -> str:
        if value in (None, ""):
            return "never"
        interval = str(value).strip()
        allowed = {"never", "weekly", "monthly"}
        if interval not in allowed:
            msg = f"GIT_BACKUP_FULL_REPACK_INTERVAL must be one of {sorted(allowed)}, got {interval!r}"
            raise ValueError(msg)
        return interval
