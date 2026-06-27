"""AI account backup configuration (ChatGPT + Claude via CloakBrowser).

Drives the Taskiq ai-backup sync job that mirrors the operator's own ChatGPT
and Claude web-account data (conversations, projects, attachments, artifacts)
to a local store using authenticated CloakBrowser sessions.

Off by default and double-gated: ``AI_BACKUP_ENABLED`` plus the per-service flag
(``AI_BACKUP_CHATGPT_ENABLED`` / ``AI_BACKUP_CLAUDE_ENABLED``) must both be true
for a service to run. The scrape path drives the providers' web products with
automation, which violates their Terms of Service; this is an eyes-open,
own-account-only, single-tenant tool. See
``docs/explanation/ai-account-backup.md`` for the full design.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_DEFAULT_HOST_ALLOWLIST = (
    "chatgpt.com",
    "chat.openai.com",
    "*.oaiusercontent.com",
    "claude.ai",
    "*.anthropic.com",
)


class AiBackupConfig(BaseModel):
    """Configuration for the AI account backup subsystem."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=False,
        validation_alias="AI_BACKUP_ENABLED",
        description=(
            "Master switch for the periodic AI account-backup Taskiq job. "
            "When false the job is not registered with the scheduler."
        ),
    )
    sync_cron: str = Field(
        default="0 5 * * *",
        validation_alias="AI_BACKUP_SYNC_CRON",
        description="UTC cron expression for the AI account-backup sync job.",
    )
    data_path: str = Field(
        default="/data/ai-backups",
        validation_alias="AI_BACKUP_DATA_PATH",
        description=(
            "Container-internal path where backed-up account data is written. "
            "Typically bind-mounted from the host."
        ),
    )

    # Per-service master switches.
    chatgpt_enabled: bool = Field(
        default=False,
        validation_alias="AI_BACKUP_CHATGPT_ENABLED",
        description="Back up the operator's ChatGPT (chatgpt.com) account when enabled.",
    )
    claude_enabled: bool = Field(
        default=False,
        validation_alias="AI_BACKUP_CLAUDE_ENABLED",
        description="Back up the operator's Claude (claude.ai) account when enabled.",
    )

    # Cadence / anti-bot shaping.
    request_delay_ms: int = Field(
        default=1500,
        ge=0,
        validation_alias="AI_BACKUP_REQUEST_DELAY_MS",
        description=(
            "Base delay between internal-API requests within a run, in milliseconds. "
            "Jitter is added on top. Keeps the request cadence below bot-scoring thresholds."
        ),
    )
    max_requests_per_run: int = Field(
        default=5000,
        ge=1,
        validation_alias="AI_BACKUP_MAX_REQUESTS_PER_RUN",
        description="Hard cap on internal-API requests per service per run (safety stop).",
    )
    download_files: bool = Field(
        default=True,
        validation_alias="AI_BACKUP_DOWNLOAD_FILES",
        description="Download conversation/project file attachments (not just metadata).",
    )
    incremental: bool = Field(
        default=True,
        validation_alias="AI_BACKUP_INCREMENTAL",
        description=(
            "Skip conversations whose update timestamp is unchanged since the last "
            "successful run instead of re-downloading everything."
        ),
    )
    host_allowlist: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_HOST_ALLOWLIST),
        validation_alias="AI_BACKUP_HOST_ALLOWLIST",
        description=(
            "Hostnames (exact or ``*.suffix`` wildcards) the backup clients may call. "
            "Every internal-API URL is validated against this list before the request. "
            "Accepts a comma-separated string or a JSON list in the env var."
        ),
    )
    claude_compliance_key: str | None = Field(
        default=None,
        validation_alias="AI_BACKUP_CLAUDE_COMPLIANCE_KEY",
        description=(
            "Optional Anthropic Compliance API key (Claude Enterprise). When set, the "
            "Claude path uses the sanctioned api.anthropic.com/v1/compliance/* surface "
            "instead of the stealth scrape. Left unset for consumer (Pro/Max) accounts."
        ),
    )

    # Health monitoring (Healthchecks.io dead-man switch).
    hc_ping_url: str | None = Field(
        default=None,
        validation_alias="AI_BACKUP_HC_PING_URL",
        description=(
            "Base Healthchecks.io ping URL for the AI account-backup job. When set, the "
            "task POSTs to {url}/start before the run, {url} on success, {url}/fail on error."
        ),
    )
    hc_ping_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        validation_alias="AI_BACKUP_HC_PING_TIMEOUT_SECONDS",
        description="HTTP timeout in seconds for each Healthchecks.io ping request.",
    )

    # Per-run Telegram notifications.
    notify_chat_id: int | None = Field(
        default=None,
        validation_alias="AI_BACKUP_NOTIFY_CHAT_ID",
        description=(
            "Telegram chat ID to notify after each run. When None (default) no "
            "notification is sent. Requires the standard bot credentials."
        ),
    )
    notify_on: str = Field(
        default="never",
        validation_alias="AI_BACKUP_NOTIFY_ON",
        description=(
            "When to send a Telegram notification: 'never' (default), 'always', or "
            "'failure' (only when a service failed or its session expired). "
            "Only used when AI_BACKUP_NOTIFY_CHAT_ID is set."
        ),
    )

    @property
    def any_service_enabled(self) -> bool:
        """True when at least one provider is switched on."""
        return self.chatgpt_enabled or self.claude_enabled

    @field_validator("sync_cron", mode="before")
    @classmethod
    def _validate_sync_cron(cls, value: Any) -> str:
        if value in (None, ""):
            return "0 5 * * *"
        cron = str(value).strip()
        if len(cron.split()) != 5:
            msg = "AI_BACKUP_SYNC_CRON must be a 5-field cron expression"
            raise ValueError(msg)
        return cron

    @field_validator("host_allowlist", mode="before")
    @classmethod
    def _validate_host_allowlist(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return list(_DEFAULT_HOST_ALLOWLIST)
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, (list, tuple)):
            return [str(part).strip() for part in value if str(part).strip()]
        msg = "AI_BACKUP_HOST_ALLOWLIST must be a comma-separated string or a list"
        raise ValueError(msg)

    @field_validator("claude_compliance_key", mode="before")
    @classmethod
    def _validate_claude_compliance_key(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value).strip() or None

    @field_validator("notify_chat_id", mode="before")
    @classmethod
    def _validate_notify_chat_id(cls, value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            msg = f"AI_BACKUP_NOTIFY_CHAT_ID must be an integer, got {value!r}"
            raise ValueError(msg) from exc

    @field_validator("notify_on", mode="before")
    @classmethod
    def _validate_notify_on(cls, value: Any) -> str:
        if value in (None, ""):
            return "never"
        mode = str(value).strip().lower()
        allowed = {"never", "always", "failure"}
        if mode not in allowed:
            msg = f"AI_BACKUP_NOTIFY_ON must be one of {sorted(allowed)}, got {mode!r}"
            raise ValueError(msg)
        return mode

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
                "AI_BACKUP_HC_PING_URL must use http or https scheme to prevent SSRF, "
                f"got scheme {parsed.scheme!r}"
            )
            raise ValueError(msg)
        return url
