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

    # HTTP / SSL tuning (mirrors gitout ssl.* and http.* config fields)
    verify_certificates: bool = Field(
        default=True,
        validation_alias="GIT_BACKUP_VERIFY_CERTIFICATES",
        description=(
            "When false, passes http.sslVerify=false to git, disabling TLS certificate "
            "verification. Mirrors gitout ssl.verify_certificates (default: true). "
            "Only set false on private infrastructure with a known-good CA."
        ),
    )
    post_buffer_size: int = Field(
        default=524_288_000,
        ge=1024,
        validation_alias="GIT_BACKUP_POST_BUFFER_SIZE",
        description=(
            "Value for git's http.postBuffer config option in bytes (default: 524 288 000 = 500 MB). "
            "Mirrors gitout http.post_buffer_size. Increase for repos that fail with "
            "'error: RPC failed; HTTP 411 Caused by: send-pack: unexpected disconnect' on large pushes."
        ),
    )
    low_speed_limit: int = Field(
        default=1000,
        ge=0,
        validation_alias="GIT_BACKUP_LOW_SPEED_LIMIT",
        description=(
            "Value for git's http.lowSpeedLimit in bytes/second (default: 1000). "
            "Mirrors gitout http.low_speed_limit. Set to 0 to disable low-speed detection. "
            "If the transfer rate drops below this value for low_speed_time seconds, git aborts."
        ),
    )
    low_speed_time: int = Field(
        default=60,
        ge=1,
        validation_alias="GIT_BACKUP_LOW_SPEED_TIME",
        description=(
            "Value for git's http.lowSpeedTime in seconds (default: 60). "
            "Mirrors gitout http.low_speed_time. Only effective when low_speed_limit > 0."
        ),
    )

    # Clone mode
    single_branch_only: bool = Field(
        default=False,
        validation_alias="GIT_BACKUP_SINGLE_BRANCH_ONLY",
        description=(
            "When true, uses git clone --bare --single-branch instead of git clone --mirror. "
            "Mirrors gitout github.clone.single_branch_only (default: false). "
            "Reduces disk usage for repositories with many branches but omits all non-default refs."
        ),
    )

    # Shallow-clone strategy (mirrors gitout large_repos.shallow_clone_* fields)
    shallow_clone_threshold_kb: int = Field(
        default=0,
        ge=0,
        validation_alias="GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB",
        description=(
            "Repository size in KB above which a shallow clone (--depth=1) is used instead of a "
            "full mirror clone (default: 0 = disabled). "
            "Gitout's default is 2 000 000 KB (2 GB); set to 0 here to keep the feature opt-in. "
            "Only applies to initial clones, not updates. Pair with shallow_clone_after_failures "
            "to restrict shallow clones to repos that also have a failure history."
        ),
    )
    shallow_clone_after_failures: int = Field(
        default=0,
        ge=0,
        validation_alias="GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES",
        description=(
            "Number of consecutive failures after which a shallow clone (--depth=1) is attempted "
            "instead of a full mirror clone (default: 0 = disabled). "
            "Gitout's default is 3; set to 0 here to keep the feature opt-in. "
            "Only applies to initial clones, not updates. When both this and "
            "shallow_clone_threshold_kb are non-zero, both conditions must be met (gitout semantics)."
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
