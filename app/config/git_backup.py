"""Git mirror backup configuration.

Drives the Taskiq git-backup sync job that mirrors GitHub repositories (gists,
starred repos, owned repos, watched repos) and arbitrary extra repos to a local
bare-clone store using the gitout engine.  The DB-persisted GitMirror table is
the primary source of repos to mirror; extra_repos provides a lightweight
override for repos that do not need a DB row (e.g. one-off external projects).

Auto-population flags:
- mirror_gists:   enumerate GET /gists for each active UserGitHubIntegration
- mirror_starred: enumerate GET /user/starred for each active integration
- mirror_owned:   enumerate GET /user/repos?affiliation=owner for each integration
- mirror_watched: enumerate GET /user/subscriptions for each integration

All four are disabled by default and can be toggled independently.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PriorityRule(BaseModel):
    """A single priority rule for mirror task ordering.

    Mirrors gitout's ``PriorityPattern`` dataclass. Patterns are matched as
    Python regex against the mirror name (``full_name``) and/or clone URL.
    The highest-priority matching rule wins; ties preserve the original
    collection order (stable sort).

    Set via ``ratatoskr.yaml`` under ``git_backup.priorities`` (a list of
    dicts). Cannot be set meaningfully via a flat env var because it is a
    structured list; ``GIT_BACKUP_PRIORITIES`` is accepted only as a sentinel
    to keep pydantic happy — prefer YAML.
    """

    model_config = ConfigDict(frozen=True)

    pattern: str = Field(description="Regex pattern matched against the mirror name or clone URL.")
    priority: int = Field(default=0, description="Higher values run first. Default 0.")
    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Per-task timeout override in seconds. When set, replaces the global "
            "GIT_BACKUP_REPO_TIMEOUT_SECONDS for tasks that match this rule "
            "(before the large-repo multiplier is applied). None = use global default."
        ),
    )


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

    # Maintenance tuning
    repack_window: int = Field(
        default=50,
        ge=1,
        validation_alias="GIT_BACKUP_REPACK_WINDOW",
        description=(
            "Value for git repack's `--window` option during full repacks (default: 50). "
            "Mirrors gitout's `maintenance.repack_window`. Higher values improve pack "
            "density at the cost of more CPU; must be >= 1."
        ),
    )
    repack_depth: int = Field(
        default=50,
        ge=1,
        validation_alias="GIT_BACKUP_REPACK_DEPTH",
        description=(
            "Value for git repack's `--depth` option during full repacks (default: 50). "
            "Mirrors gitout's `maintenance.repack_depth`. Higher values improve pack "
            "density at the cost of more CPU; must be >= 1."
        ),
    )

    # Storage health
    circuit_breaker_threshold: int = Field(
        default=3,
        ge=1,
        validation_alias="GIT_BACKUP_CIRCUIT_BREAKER_THRESHOLD",
        description=(
            "Number of consecutive STORAGE_ERROR failures that trip the storage "
            "circuit breaker, aborting the remainder of the sync run (default: 3). "
            "Mirrors gitout's `StorageCircuitBreaker(threshold=...)`. Once open the "
            "breaker stays open for the current run; it resets on the next run."
        ),
    )
    preflight_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        validation_alias="GIT_BACKUP_PREFLIGHT_TIMEOUT_SECONDS",
        description=(
            "Timeout in seconds for the preflight storage write/read/delete check "
            "that runs before each sync (default: 10.0 s). If the sentinel write "
            "takes longer than this, the sync is aborted with a storage error."
        ),
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
            "Hours to wait before retrying a repo that has exceeded max_consecutive_failures."
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
    ssl_ca_info: str | None = Field(
        default=None,
        validation_alias="GIT_BACKUP_SSL_CA_INFO",
        description=(
            "Path to a custom CA bundle file (PEM) passed to git via `http.sslCAInfo`. "
            "When set, git uses this CA bundle instead of its compiled-in bundle to "
            "verify TLS certificates. Useful when mirroring from servers signed by a "
            "private or internal CA. When None (default), no flag is injected and git "
            "uses its default CA bundle."
        ),
    )
    http_version: str = Field(
        default="HTTP/1.1",
        validation_alias="GIT_BACKUP_HTTP_VERSION",
        description=(
            "HTTP protocol version passed to git via `http.version`. "
            "Accepted values: `HTTP/1.1` (default, matching gitout's default) or `HTTP/2`. "
            "When `HTTP/1.1`, git is forced to HTTP/1.1 for all operations. "
            "When `HTTP/2`, git may negotiate HTTP/2 with the server (subject to TLS "
            "ALPN). The per-run `force_http1` flag (set by the retry policy on "
            "HTTP2_ERROR failures) always overrides this setting and forces HTTP/1.1."
        ),
    )
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

    # README semantic indexing
    index_readmes: bool = Field(
        default=False,
        validation_alias="GIT_BACKUP_INDEX_READMES",
        description=(
            "When true, index the README of each successfully-synced mirror "
            "(with repository_id IS NULL) into Qdrant for semantic search. "
            "Requires the embedding service and Qdrant vector store to be configured. "
            "Indexing is best-effort and never blocks or fails the backup sync."
        ),
    )

    reconcile_readmes: bool = Field(
        default=False,
        validation_alias="GIT_BACKUP_RECONCILE_READMES",
        description=(
            "When true, after each sync run reconcile the git_mirror README vectors "
            "in Qdrant against the database: delete orphaned points (deleted, excluded, "
            "or now-GitHub-linked mirrors) and recreate missing points (force re-index). "
            "Requires index_readmes infrastructure (embedding + Qdrant). Best-effort; "
            "never blocks or fails the backup sync."
        ),
    )

    # GitHub repository auto-population (starred / owned / watched)
    mirror_starred: bool = Field(
        default=False,
        validation_alias="GIT_BACKUP_MIRROR_STARRED",
        description=(
            "When true, enumerate all starred repositories for each user with an active GitHub "
            "integration and upsert a GitMirror row per repo so it is cloned by the regular "
            "mirror sync. Clone URLs use the HTTPS form "
            "https://github.com/<owner>/<name>.git. size_kb is populated from the GitHub "
            "repo size field so large-repo timeout scaling applies on the first clone. "
            "Disabled by default."
        ),
    )
    mirror_owned: bool = Field(
        default=False,
        validation_alias="GIT_BACKUP_MIRROR_OWNED",
        description=(
            "When true, enumerate all repositories owned by each user with an active GitHub "
            "integration (GET /user/repos?affiliation=owner) and upsert a GitMirror row per repo. "
            "Clone URLs use the HTTPS form https://github.com/<owner>/<name>.git. size_kb is "
            "populated from the GitHub repo size field. Disabled by default."
        ),
    )
    mirror_watched: bool = Field(
        default=False,
        validation_alias="GIT_BACKUP_MIRROR_WATCHED",
        description=(
            "When true, enumerate all repositories watched by each user with an active GitHub "
            "integration (GET /user/subscriptions) and upsert a GitMirror row per repo. "
            "Clone URLs use the HTTPS form https://github.com/<owner>/<name>.git. size_kb is "
            "populated from the GitHub repo size field. Disabled by default."
        ),
    )

    # Gist mirroring
    mirror_gists: bool = Field(
        default=False,
        validation_alias="GIT_BACKUP_MIRROR_GISTS",
        description=(
            "When true, enumerate all gists for each user with an active GitHub integration "
            "and upsert a GitMirror row (source=github) per gist so it is cloned by the "
            "regular mirror sync. Gist clone URLs use the form "
            "https://gist.github.com/<id>.git. Disabled by default."
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

    # Priority rules for task ordering (opt-in, default empty = no reordering)
    priorities: list[PriorityRule] = Field(
        default_factory=list,
        validation_alias="GIT_BACKUP_PRIORITIES",
        description=(
            "Ordered list of priority rules for mirror task ordering and per-task timeout "
            "overrides. Each rule has a ``pattern`` (Python regex matched against the mirror "
            "name or clone URL), a ``priority`` int (higher = runs first; default 0), and an "
            "optional ``timeout_seconds`` override. The highest-priority matching rule wins; "
            "the task list is sorted by priority DESC (stable) before workers are started. "
            "Empty list (default) = no reordering, current behavior unchanged. "
            "Set via ratatoskr.yaml under git_backup.priorities as a list of dicts."
        ),
    )

    # Static ignore list (opt-in, default empty = nothing ignored)
    ignore: list[str] = Field(
        default_factory=list,
        validation_alias="GIT_BACKUP_IGNORE",
        description=(
            "List of regex/substring patterns. Any mirror whose name or clone URL matches "
            "at least one pattern is excluded from the current sync run. The filter runs "
            "in _collect_tasks and applies to both DB-backed and config extra_repos targets. "
            "Empty list (default) = nothing ignored, current behavior unchanged. "
            "Set via ratatoskr.yaml or as a JSON-encoded list in the env var "
            '(e.g. GIT_BACKUP_IGNORE=\'["some-fork", "private/.*"]\').'
        ),
    )

    # Health monitoring (Healthchecks.io dead-man-switch)
    prune_excluded_days: int = Field(
        default=0,
        ge=0,
        validation_alias="GIT_BACKUP_PRUNE_EXCLUDED_DAYS",
        description=(
            "When > 0, mirrors with status=EXCLUDED whose excluded_at timestamp is older "
            "than this many days are automatically pruned during each sync run: their Qdrant "
            "point is deleted (best-effort), the on-disk bare clone is removed (best-effort, "
            "only if mirror_path resolves inside GIT_BACKUP_DATA_PATH), and the DB row is "
            "deleted.  0 = disabled (default).  The prune sweep runs after perform_sync and "
            "never blocks or fails the backup task."
        ),
    )

    hc_ping_url: str | None = Field(
        default=None,
        validation_alias="GIT_BACKUP_HC_PING_URL",
        description=(
            "Base Healthchecks.io (or compatible) ping URL for the git-backup sync job "
            "(e.g. https://hc-ping.com/<uuid>). When set, the task POSTs to {url}/start "
            "before the sync begins, to {url} on success, and to {url}/fail on exception. "
            "When None or empty, health pinging is disabled."
        ),
    )
    hc_ping_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        validation_alias="GIT_BACKUP_HC_PING_TIMEOUT_SECONDS",
        description="HTTP timeout in seconds for each Healthchecks.io ping request.",
    )

    # Failure propagation (exit_on_failure)
    exit_on_failure: bool = Field(
        default=False,
        validation_alias="GIT_BACKUP_EXIT_ON_FAILURE",
        description=(
            "When true AND the sync summary reports at least one failed repo, "
            "the Taskiq task raises a RuntimeError at the end of the try block "
            "(after index/reconcile/metrics/notify steps have run). This causes "
            "Taskiq to record the run as failed and fires the healthcheck failure ping. "
            "Default false = current behavior (task always completes as success "
            "regardless of how many repos failed). Opt-in."
        ),
    )

    # Metrics export
    metrics_export_path: str | None = Field(
        default=None,
        validation_alias="GIT_BACKUP_METRICS_EXPORT_PATH",
        description=(
            "When set, after each sync run a per-run metrics record is appended to "
            "this file. The format is determined by GIT_BACKUP_METRICS_FORMAT. "
            "File I/O errors are logged at WARNING and swallowed — the task outcome "
            "is never affected. Default None = disabled."
        ),
    )
    metrics_format: str = Field(
        default="json",
        validation_alias="GIT_BACKUP_METRICS_FORMAT",
        description=(
            "Format for the metrics export file. Accepted values: 'json' (JSONL, "
            "one JSON object per line appended on each run) or 'csv' (one row "
            "appended; header written when the file is new/empty). "
            "Only used when GIT_BACKUP_METRICS_EXPORT_PATH is set."
        ),
    )

    # Per-run Telegram notifications
    notify_chat_id: int | None = Field(
        default=None,
        validation_alias="GIT_BACKUP_NOTIFY_CHAT_ID",
        description=(
            "Telegram chat ID to send a completion notification to after each sync "
            "run. When None (default), no notification is sent. "
            "Requires the standard Telegram bot credentials (API_ID, API_HASH, "
            "BOT_TOKEN) to be configured."
        ),
    )
    notify_on: str = Field(
        default="never",
        validation_alias="GIT_BACKUP_NOTIFY_ON",
        description=(
            "When to send a Telegram notification. Accepted values: "
            "'never' (default, no notifications), "
            "'always' (send on every run), "
            "'failure' (send only when summary.failed > 0). "
            "Only used when GIT_BACKUP_NOTIFY_CHAT_ID is set."
        ),
    )

    @field_validator("metrics_export_path", mode="before")
    @classmethod
    def _validate_metrics_export_path(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value).strip() or None

    @field_validator("metrics_format", mode="before")
    @classmethod
    def _validate_metrics_format(cls, value: Any) -> str:
        if value in (None, ""):
            return "json"
        fmt = str(value).strip().lower()
        allowed = {"json", "csv"}
        if fmt not in allowed:
            msg = f"GIT_BACKUP_METRICS_FORMAT must be one of {sorted(allowed)}, got {fmt!r}"
            raise ValueError(msg)
        return fmt

    @field_validator("notify_chat_id", mode="before")
    @classmethod
    def _validate_notify_chat_id(cls, value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            msg = f"GIT_BACKUP_NOTIFY_CHAT_ID must be an integer, got {value!r}"
            raise ValueError(msg) from exc

    @field_validator("notify_on", mode="before")
    @classmethod
    def _validate_notify_on(cls, value: Any) -> str:
        if value in (None, ""):
            return "never"
        mode = str(value).strip().lower()
        allowed = {"never", "always", "failure"}
        if mode not in allowed:
            msg = f"GIT_BACKUP_NOTIFY_ON must be one of {sorted(allowed)}, got {mode!r}"
            raise ValueError(msg)
        return mode

    @field_validator("ssl_ca_info", mode="before")
    @classmethod
    def _validate_ssl_ca_info(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value).strip() or None

    @field_validator("http_version", mode="before")
    @classmethod
    def _validate_http_version(cls, value: Any) -> str:
        if value in (None, ""):
            return "HTTP/1.1"
        version = str(value).strip()
        allowed = {"HTTP/1.1", "HTTP/2"}
        if version not in allowed:
            msg = f"GIT_BACKUP_HTTP_VERSION must be one of {sorted(allowed)}, got {version!r}"
            raise ValueError(msg)
        return version

    @field_validator("hc_ping_url", mode="before")
    @classmethod
    def _validate_hc_ping_url(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        url = str(value).strip()
        if not url:
            return None
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            msg = (
                f"GIT_BACKUP_HC_PING_URL must use http or https scheme to prevent SSRF, "
                f"got scheme {parsed.scheme!r}"
            )
            raise ValueError(msg)
        return url

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
