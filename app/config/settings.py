from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from typing import Self

from pydantic import AliasChoices, BaseModel, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .academic import AcademicConfig
from .adaptive_timeout import AdaptiveTimeoutConfig
from .ai_backup import AiBackupConfig
from .api import ApiLimitsConfig, AuthConfig, SyncConfig
from .background import BackgroundProcessorConfig
from .backup import BackupConfig
from .circuit_breaker import CircuitBreakerConfig
from .content import ContentLimitsConfig
from .database import DatabaseConfig
from .deployment import DeploymentConfig
from .digest import ChannelDigestConfig
from .email import EmailConfig
from .firecrawl import FirecrawlConfig
from .git_backup import GitBackupConfig
from .github import GitHubConfig
from .import_export import ImportConfig
from .integrations import (
    BatchAnalysisConfig,
    EmbeddingConfig,
    McpConfig,
    QdrantConfig,
    VectorReconcileConfig,
    WebSearchConfig,
)
from .langgraph import LangGraphCheckpointConfig
from .llm import (
    DirectAnthropicConfig,
    DirectOllamaConfig,
    DirectOpenAIConfig,
    LLMUsageBudgetConfig,
    ModelRoutingConfig,
    OpenRouterConfig,
)
from .media import AttachmentConfig, YouTubeConfig  # noqa: TC001
from .otel import OtelConfig, SentryConfig
from .push import PushNotificationConfig
from .redis import RedisConfig
from .retention import RetentionConfig
from .rss import RSSConfig
from .runtime import RuntimeConfig
from .scraper import ScraperConfig
from .signal_ingestion import SignalIngestionConfig
from .social import SocialConfig
from .telegram import TelegramConfig, TelegramLimitsConfig
from .transcription import TranscriptionConfig
from .tts import ElevenLabsConfig
from .twitter import TwitterConfig
from .x_bookmarks import XBookmarksConfig

logger = get_logger(__name__)
_CONFIG_CACHE_LOCK = threading.Lock()
_CONFIG_CACHE: dict[bool, AppConfig] = {}

_DEPRECATED_SCRAPER_ENV_RENAMES = {
    "SCRAPLING_ENABLED": "SCRAPER_SCRAPLING_ENABLED",
    "SCRAPLING_TIMEOUT_SEC": "SCRAPER_SCRAPLING_TIMEOUT_SEC",
    "SCRAPLING_STEALTH_FALLBACK": "SCRAPER_SCRAPLING_STEALTH_FALLBACK",
    "SCRAPER_DIRECT_HTTP_ENABLED": "SCRAPER_DIRECT_HTML_ENABLED",
}

_DEPRECATED_PHASE1_ENV_VARS = {
    "MIGRATION_SHADOW_MODE_ENABLED": "removed; M3 runtime bridge is authoritative",
    "MIGRATION_SHADOW_MODE_TIMEOUT_MS": "removed; M3 runtime bridge is authoritative",
    "MIGRATION_SHADOW_MODE_SAMPLE_RATE": "removed; M3 runtime bridge is authoritative",
    "MIGRATION_SHADOW_MODE_MAX_DIFFS": "removed; M3 runtime bridge is authoritative",
    "MIGRATION_SHADOW_MODE_EMIT_MATCH_LOGS": "removed; M3 runtime bridge is authoritative",
}


def raise_on_deprecated_scraper_env_vars() -> None:
    present: dict[str, str] = {}
    for old_name, new_name in _DEPRECATED_SCRAPER_ENV_RENAMES.items():
        if old_name in os.environ:
            present[old_name] = new_name
    for name, replacement in _DEPRECATED_PHASE1_ENV_VARS.items():
        if name in os.environ:
            present[name] = replacement

    env_file_path = Path(".env")
    if env_file_path.exists():
        for line in env_file_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in _DEPRECATED_SCRAPER_ENV_RENAMES:
                present[key] = _DEPRECATED_SCRAPER_ENV_RENAMES[key]
            if key in _DEPRECATED_PHASE1_ENV_VARS:
                present[key] = _DEPRECATED_PHASE1_ENV_VARS[key]

    if not present:
        return

    details = "; ".join(f"{old} -> {new}" for old, new in sorted(present.items()))
    msg = f"Deprecated environment variables detected. Remove or replace these variables: {details}"
    raise RuntimeError(msg)


_FIRST_RUN_REQUIRED_ENV = (
    "API_ID",
    "API_HASH",
    "BOT_TOKEN",
    "ALLOWED_USER_IDS",
    "OPENROUTER_API_KEY",
    "DATABASE_URL",
)


def _read_dotenv_keys(path: Path = Path(".env")) -> set[str]:
    if not path.is_file():
        return set()
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip())
    return keys


def _format_required_config_error(missing: tuple[str, ...]) -> str:
    joined = ", ".join(missing)
    return (
        f"Missing required first-run configuration: {joined}. "
        "Copy .env.example to .env and fill these names. "
        "Optional power-user settings belong in ratatoskr.yaml; see "
        "docs/reference/config-file.md."
    )


def _effective_config_summary(config: AppConfig) -> dict[str, Any]:
    """Return a small, redacted startup summary of effective config choices."""
    return {
        "llm_provider": config.runtime.llm_provider,
        "openrouter_model": config.openrouter.model,
        "scraper_profile": config.scraper.profile,
        "scraper_provider_order": list(config.scraper.provider_order),
        "youtube_enabled": config.youtube.enabled,
        "twitter_enabled": config.twitter.enabled,
        "mcp_enabled": config.mcp.enabled,
        "jwt_auth_configured": bool(config.runtime.jwt_secret_key),
        "database_dsn": _redact_database_dsn(config.database.dsn),
    }


def _redact_database_dsn(dsn: str) -> str:
    if "@" not in dsn:
        return dsn
    prefix, suffix = dsn.rsplit("@", 1)
    if ":" not in prefix:
        return f"...@{suffix}"
    scheme_user, _password = prefix.rsplit(":", 1)
    return f"{scheme_user}:***@{suffix}"


@dataclass(frozen=True)
class AppConfig:
    telegram: TelegramConfig
    firecrawl: FirecrawlConfig
    openrouter: OpenRouterConfig
    youtube: YouTubeConfig
    attachment: AttachmentConfig
    runtime: RuntimeConfig
    telegram_limits: TelegramLimitsConfig
    database: DatabaseConfig
    content_limits: ContentLimitsConfig
    vector_store: QdrantConfig
    redis: RedisConfig
    api_limits: ApiLimitsConfig
    auth: AuthConfig
    sync: SyncConfig
    background: BackgroundProcessorConfig
    circuit_breaker: CircuitBreakerConfig
    web_search: WebSearchConfig
    adaptive_timeout: AdaptiveTimeoutConfig
    batch_analysis: BatchAnalysisConfig
    openai: DirectOpenAIConfig = field(default_factory=DirectOpenAIConfig)
    anthropic: DirectAnthropicConfig = field(default_factory=DirectAnthropicConfig)
    ollama: DirectOllamaConfig = field(default_factory=DirectOllamaConfig)
    llm_usage_budget: LLMUsageBudgetConfig = field(default_factory=LLMUsageBudgetConfig)
    twitter: TwitterConfig = field(default_factory=TwitterConfig)
    digest: ChannelDigestConfig = field(default_factory=ChannelDigestConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    scraper: ScraperConfig = field(default_factory=ScraperConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    tts: ElevenLabsConfig = field(default_factory=ElevenLabsConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    academic: AcademicConfig = field(default_factory=AcademicConfig)
    push: PushNotificationConfig = field(default_factory=PushNotificationConfig)
    model_routing: ModelRoutingConfig = field(default_factory=ModelRoutingConfig)
    rss: RSSConfig = field(default_factory=RSSConfig)
    signal_ingestion: SignalIngestionConfig = field(default_factory=SignalIngestionConfig)
    social: SocialConfig = field(default_factory=SocialConfig)
    otel: OtelConfig = field(default_factory=OtelConfig)
    sentry: SentryConfig = field(default_factory=SentryConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    vector_reconcile: VectorReconcileConfig = field(default_factory=VectorReconcileConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    import_export: ImportConfig = field(default_factory=ImportConfig)
    deployment: DeploymentConfig = field(default_factory=DeploymentConfig)
    x_bookmarks: XBookmarksConfig = field(default_factory=XBookmarksConfig)
    git_backup: GitBackupConfig = field(default_factory=GitBackupConfig)
    ai_backup: AiBackupConfig = field(default_factory=AiBackupConfig)
    langgraph_checkpoint: LangGraphCheckpointConfig = field(
        default_factory=LangGraphCheckpointConfig
    )


class Settings(BaseSettings):
    """Application settings loaded automatically from environment variables.

    Uses pydantic-settings for automatic environment variable loading.
    Nested models are populated by matching validation_alias on each field.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    allow_stub_telegram: bool = Field(default=False, exclude=True)
    telegram: TelegramConfig
    # FirecrawlConfig is fully optional in a self-hosted-only deployment;
    # default_factory lets the bot start when no FIRECRAWL_* env vars are set.
    firecrawl: FirecrawlConfig = Field(default_factory=FirecrawlConfig)
    openrouter: OpenRouterConfig
    openai: DirectOpenAIConfig = Field(default_factory=DirectOpenAIConfig)
    anthropic: DirectAnthropicConfig = Field(default_factory=DirectAnthropicConfig)
    ollama: DirectOllamaConfig = Field(default_factory=DirectOllamaConfig)
    llm_usage_budget: LLMUsageBudgetConfig = Field(default_factory=LLMUsageBudgetConfig)
    youtube: YouTubeConfig
    attachment: AttachmentConfig
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    telegram_limits: TelegramLimitsConfig = Field(default_factory=TelegramLimitsConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    content_limits: ContentLimitsConfig = Field(default_factory=ContentLimitsConfig)
    vector_store: QdrantConfig = Field(default_factory=QdrantConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    api_limits: ApiLimitsConfig = Field(default_factory=ApiLimitsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    background: BackgroundProcessorConfig = Field(default_factory=BackgroundProcessorConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    adaptive_timeout: AdaptiveTimeoutConfig = Field(default_factory=AdaptiveTimeoutConfig)
    batch_analysis: BatchAnalysisConfig = Field(default_factory=BatchAnalysisConfig)
    twitter: TwitterConfig = Field(default_factory=TwitterConfig)
    digest: ChannelDigestConfig = Field(default_factory=ChannelDigestConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    scraper: ScraperConfig = Field(default_factory=ScraperConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    tts: ElevenLabsConfig = Field(default_factory=ElevenLabsConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    academic: AcademicConfig = Field(default_factory=AcademicConfig)
    push: PushNotificationConfig = Field(default_factory=PushNotificationConfig)
    model_routing: ModelRoutingConfig = Field(default_factory=ModelRoutingConfig)
    rss: RSSConfig = Field(default_factory=RSSConfig)
    signal_ingestion: SignalIngestionConfig = Field(default_factory=SignalIngestionConfig)
    social: SocialConfig = Field(default_factory=SocialConfig)
    otel: OtelConfig = Field(default_factory=OtelConfig)
    sentry: SentryConfig = Field(default_factory=SentryConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    vector_reconcile: VectorReconcileConfig = Field(default_factory=VectorReconcileConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    import_export: ImportConfig = Field(default_factory=ImportConfig)
    deployment: DeploymentConfig = Field(default_factory=DeploymentConfig)
    x_bookmarks: XBookmarksConfig = Field(default_factory=XBookmarksConfig)
    git_backup: GitBackupConfig = Field(default_factory=GitBackupConfig)
    ai_backup: AiBackupConfig = Field(default_factory=AiBackupConfig)
    langgraph_checkpoint: LangGraphCheckpointConfig = Field(
        default_factory=LangGraphCheckpointConfig
    )

    @model_validator(mode="before")
    @classmethod
    def _build_nested_from_env(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Build nested config objects from flat environment variables.

        pydantic-settings passes constructor args as data, but environment variables
        need to be read from os.environ separately for proper nested model population.
        This validator merges both sources, with constructor args taking precedence.
        """
        if not isinstance(data, dict):
            return data

        result = dict(data)

        # Load YAML config layers.
        from app.config._secret_marker import (
            collect_secret_env_names,
            filter_yaml_to_non_secrets,
        )
        from app.config.config_file import load_ratatoskr_yaml

        config_file_data = load_ratatoskr_yaml(cls)

        # Precedence:
        #
        #   non-secret YAML  >  os.environ  >  .env / constructor args  >  defaults
        #   secret env       >  defaults                (YAML secret keys ignored)
        #
        # Rationale: secrets belong in .env (or process env, e.g. Docker
        # secrets); operational tunables live in ratatoskr.yaml so the YAML
        # file is the source of truth on disk and survives env-var drift
        # between hosts. Secrets that accidentally land in YAML are dropped
        # and logged so credentials cannot leak into a committed file.
        env_data: dict[str, Any] = dict(os.environ)
        base_source: dict[str, Any] = {**data, **env_data}
        cls._fail_on_deprecated_envs(base_source)

        secret_env_names = collect_secret_env_names(cls)
        ratatoskr_yaml_non_secret, ratatoskr_yaml_secret = filter_yaml_to_non_secrets(
            config_file_data, secret_env_names
        )
        ignored_yaml_secrets = sorted(ratatoskr_yaml_secret)
        if ignored_yaml_secrets:
            logger.warning(
                "yaml_secret_keys_ignored",
                extra={
                    "keys": ignored_yaml_secrets,
                    "guidance": "Secret-marked fields must live in .env, not YAML.",
                },
            )

        # YAML wins for non-secret keys: layered AFTER env so it overrides.
        merged_source: dict[str, Any] = {
            **base_source,
            **ratatoskr_yaml_non_secret,
        }

        for field_name, field_info in cls.model_fields.items():
            if field_name in ("allow_stub_telegram",):
                continue

            annotation = field_info.annotation
            if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
                continue

            nested_data: dict[str, Any] = {}
            nested_model: type[BaseModel] = annotation

            for nested_field_name, nested_field in nested_model.model_fields.items():
                env_value = cls._resolve_env_value(merged_source, nested_field)
                if env_value is not None:
                    nested_data[nested_field_name] = env_value

            if nested_data:
                if field_name in result and isinstance(result[field_name], dict):
                    result[field_name] = {**nested_data, **result[field_name]}
                else:
                    result[field_name] = nested_data

        return result

    @staticmethod
    def _fail_on_deprecated_envs(source: dict[str, Any]) -> None:
        deprecated: list[str] = []
        for old_name, new_name in _DEPRECATED_SCRAPER_ENV_RENAMES.items():
            if old_name in source:
                deprecated.append(f"{old_name} -> {new_name}")
        for name, replacement in _DEPRECATED_PHASE1_ENV_VARS.items():
            if name in source:
                deprecated.append(f"{name} -> {replacement}")
        if not deprecated:
            return
        msg = (
            "Deprecated environment variables detected. "
            "Remove or replace these variables: " + "; ".join(deprecated)
        )
        raise RuntimeError(msg)

    @staticmethod
    def _resolve_env_value(data: dict[str, Any], field: Any) -> Any | None:
        """Resolve environment variable value for a field using its aliases."""
        aliases: list[str] = []
        alias = field.validation_alias
        if isinstance(alias, AliasChoices):
            for choice in alias.choices:
                if isinstance(choice, str):
                    aliases.append(choice)
        elif isinstance(alias, str):
            aliases.append(alias)
        if field.alias:
            aliases.append(field.alias)

        for name in aliases:
            if name in data:
                return data[name]
        return None

    @model_validator(mode="after")
    def _ensure_allowed_users(self) -> Self:
        if not self.allow_stub_telegram and not self.telegram.allowed_user_ids:
            msg = (
                "ALLOWED_USER_IDS must contain at least one Telegram user ID; "
                "set the environment variable to a comma-separated list."
            )
            raise RuntimeError(msg)
        return self

    @model_validator(mode="after")
    def _ensure_production_redis_rate_limiting(self) -> Self:
        """Require Redis-backed rate limiting in production.

        In-memory rate limiting is process-local: limits are not shared across
        workers or across restarts. This is unacceptable in production.
        """
        if not self.deployment.is_production_mode:
            return self
        if self.deployment.rate_limit_redis_override:
            raise RuntimeError(
                "Production/public deployment refuses RATE_LIMIT_REDIS_OVERRIDE=true. "
                "Auth rate limiting must use shared Redis state because in-memory limits "
                "are per-process and reset on restart. Set RATE_LIMIT_REDIS_OVERRIDE=false, "
                "REDIS_ENABLED=true, and REDIS_REQUIRED=true."
            )
        if not self.redis.enabled:
            raise RuntimeError(
                "Production deployment requires Redis for rate limiting "
                "(REDIS_ENABLED=true). "
                "In-memory rate limiting is per-process and unsafe for multi-worker "
                "deployments. Set REDIS_ENABLED=true and REDIS_REQUIRED=true."
            )
        if not self.redis.required:
            raise RuntimeError(
                "Production deployment requires REDIS_REQUIRED=true. "
                "Without it, rate limiting silently falls back to in-memory state "
                "when Redis is unavailable, making limits ineffective across workers. "
                "Set REDIS_REQUIRED=true."
            )
        return self

    @model_validator(mode="after")
    def _ensure_production_client_allowlist(self) -> Self:
        """Require explicit client-ID posture in production/public deployments."""
        if self.auth.allowed_client_ids:
            return self
        if self.auth.allow_any_client_id:
            logger.warning(
                "auth_allow_any_client_id_override_active",
                extra={
                    "app_env": self.deployment.env,
                    "api_public_exposure": self.deployment.api_public_exposure,
                    "warning": (
                        "AUTH_ALLOW_ANY_CLIENT_ID=true: every syntactically valid "
                        "client_id can authenticate while ALLOWED_CLIENT_IDS is empty."
                    ),
                },
            )
            return self
        if self.deployment.is_production_mode:
            raise RuntimeError(
                "Production deployment requires ALLOWED_CLIENT_IDS to list authorized "
                "client applications. Empty ALLOWED_CLIENT_IDS accepts any valid client_id "
                "and is unsafe for public deployments. Set ALLOWED_CLIENT_IDS to a "
                "comma-separated allowlist, or set AUTH_ALLOW_ANY_CLIENT_ID=true to "
                "explicitly accept broad client access."
            )
        logger.warning(
            "auth_client_allowlist_empty_development",
            extra={
                "app_env": self.deployment.env,
                "api_public_exposure": self.deployment.api_public_exposure,
                "warning": (
                    "ALLOWED_CLIENT_IDS is empty; every syntactically valid client_id "
                    "is accepted. This is intended only for local/development use."
                ),
            },
        )
        return self

    @model_validator(mode="after")
    def _ensure_production_github_token_encryption_key(self) -> Self:
        """Require a GitHub token encryption key before production/public startup."""
        if self.deployment.is_production_mode and self.github.token_encryption_key is None:
            raise RuntimeError(
                "Production deployment requires GITHUB_TOKEN_ENCRYPTION_KEY. "
                "Stored GitHub PAT/OAuth credentials must be encrypted with a "
                "deployment-owned Fernet key before GitHub auth or sync can be used. Generate one "
                "with: python tools/scripts/generate_github_encryption_key.py."
            )
        return self

    def as_app_config(self) -> AppConfig:
        return AppConfig(
            telegram=self.telegram,
            firecrawl=self.firecrawl,
            openrouter=self.openrouter,
            openai=self.openai,
            anthropic=self.anthropic,
            ollama=self.ollama,
            youtube=self.youtube,
            attachment=self.attachment,
            runtime=self.runtime,
            telegram_limits=self.telegram_limits,
            database=self.database,
            content_limits=self.content_limits,
            vector_store=self.vector_store,
            redis=self.redis,
            api_limits=self.api_limits,
            auth=self.auth,
            sync=self.sync,
            background=self.background,
            circuit_breaker=self.circuit_breaker,
            web_search=self.web_search,
            adaptive_timeout=self.adaptive_timeout,
            batch_analysis=self.batch_analysis,
            twitter=self.twitter,
            digest=self.digest,
            mcp=self.mcp,
            scraper=self.scraper,
            embedding=self.embedding,
            tts=self.tts,
            transcription=self.transcription,
            academic=self.academic,
            push=self.push,
            model_routing=self.model_routing,
            rss=self.rss,
            signal_ingestion=self.signal_ingestion,
            social=self.social,
            otel=self.otel,
            sentry=self.sentry,
            github=self.github,
            vector_reconcile=self.vector_reconcile,
            retention=self.retention,
            backup=self.backup,
            import_export=self.import_export,
            deployment=self.deployment,
            x_bookmarks=self.x_bookmarks,
            git_backup=self.git_backup,
            ai_backup=self.ai_backup,
            langgraph_checkpoint=self.langgraph_checkpoint,
        )


def clear_config_cache() -> None:
    """Clear cached AppConfig instances.

    Tests mutate environment variables between runs, so they need a way to
    invalidate the process-level config cache.
    """
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE.clear()


def _build_config(*, allow_stub_telegram: bool) -> AppConfig:
    """Build a fresh immutable AppConfig from environment variables."""
    overrides: dict[str, Any] = {"allow_stub_telegram": allow_stub_telegram}
    using_stub_telegram = False
    raise_on_deprecated_scraper_env_vars()

    if not allow_stub_telegram:
        available = set(os.environ) | _read_dotenv_keys()
        missing = tuple(name for name in _required_first_run_env_names() if name not in available)
        if missing:
            raise RuntimeError(_format_required_config_error(missing))

    if allow_stub_telegram:
        telegram_overrides: dict[str, Any] = {}
        if not os.getenv("API_ID"):
            telegram_overrides["api_id"] = "1"
            using_stub_telegram = True
        if not os.getenv("API_HASH"):
            telegram_overrides["api_hash"] = "test_api_hash_placeholder_value___"
            using_stub_telegram = True
        if not os.getenv("BOT_TOKEN"):
            telegram_overrides["bot_token"] = "1000000000:TESTTOKENPLACEHOLDER1234567890ABC"
            using_stub_telegram = True
        if telegram_overrides:
            overrides["telegram"] = telegram_overrides

    if _configured_llm_provider_from_sources() != "openrouter":
        overrides["openrouter"] = _direct_provider_openrouter_placeholder()

    try:
        settings = Settings(**overrides)
    except (ValidationError, RuntimeError) as exc:  # pragma: no cover - defensive
        # Only flag a variable as missing when its name actually appears in the
        # underlying error. The previous version unconditionally added
        # ALLOWED_USER_IDS to the report, which masked unrelated failures
        # (e.g. a missing DATABASE_URL) behind a misleading message.
        exc_text = str(exc)
        missing = tuple(name for name in _required_first_run_env_names() if name in exc_text)
        if missing:
            msg = _format_required_config_error(missing)
        else:
            msg = f"Configuration validation failed: {exc}"
        raise RuntimeError(msg) from exc

    config = settings.as_app_config()

    logger.info("effective_config_loaded", extra={"config": _effective_config_summary(config)})

    if using_stub_telegram:
        logger.warning(
            "Using stub Telegram credentials: real API_ID/API_HASH/BOT_TOKEN were not provided"
        )

    return config


def _configured_llm_provider_from_sources() -> str:
    from app.config.config_file import load_ratatoskr_yaml

    yaml_data = load_ratatoskr_yaml(Settings)
    raw_value = yaml_data.get("LLM_PROVIDER", os.getenv("LLM_PROVIDER", "openrouter"))
    return str(raw_value).strip().lower() or "openrouter"


def _required_first_run_env_names() -> tuple[str, ...]:
    required = tuple(name for name in _FIRST_RUN_REQUIRED_ENV if name != "OPENROUTER_API_KEY")
    if _configured_llm_provider_from_sources() == "openrouter":
        return (*required, "OPENROUTER_API_KEY")
    return required


def _direct_provider_openrouter_placeholder() -> dict[str, Any]:
    return {
        "api_key": "direct-provider-placeholder",
        "model": "direct/provider-placeholder",
        "fallback_models": (),
        "flash_model": "direct/provider-placeholder",
        "flash_fallback_models": (),
        "long_context_model": "direct/provider-placeholder",
        "temperature": 0.2,
        "enable_stats": False,
        "enable_structured_outputs": True,
        "structured_output_mode": "json_object",
        "require_parameters": False,
        "auto_fallback_structured": False,
        "max_response_size_mb": 10,
        "enable_prompt_caching": False,
        "prompt_cache_ttl": "ephemeral",
        "prompt_cache_ttl_anthropic": "1h",
        "cache_system_prompt": False,
        "cache_large_content_threshold": 4096,
        "transport_retry_max_attempts": 3,
        "transport_retry_min_wait_sec": 0.5,
        "transport_retry_max_wait_sec": 5.0,
    }


def load_config(*, allow_stub_telegram: bool = False) -> AppConfig:
    """Load application configuration from environment variables.

    Uses pydantic-settings to automatically load from:
    1. Environment variables
    2. .env file (if present)

    Args:
        allow_stub_telegram: If True, use stub Telegram credentials when not provided.
                           Useful for testing and CLI tools that don't need real credentials.

    Returns:
        Immutable AppConfig instance with all configuration sections.

    Raises:
        RuntimeError: If configuration validation fails.
    """
    with _CONFIG_CACHE_LOCK:
        cached = _CONFIG_CACHE.get(allow_stub_telegram)
        if cached is not None:
            return cached

        config = _build_config(allow_stub_telegram=allow_stub_telegram)
        _CONFIG_CACHE[allow_stub_telegram] = config
        return config


class ConfigHelper:
    """Helper class for accessing configuration values from environment variables."""

    @staticmethod
    def get(key: str, default: str | None = None) -> str:
        """Get configuration value from environment variable."""
        value = os.getenv(key)
        if value is None:
            if default is None:
                raise ValueError(f"Configuration key '{key}' not found and no default provided")
            return default
        return value

    @staticmethod
    def get_allowed_user_ids() -> tuple[int, ...]:
        """Get list of allowed Telegram user IDs.

        Delegates to the validated AppConfig so tests that monkeypatch the
        cached config object see consistent behavior in auth checks.
        allow_stub_telegram=True matches the lenient lookup path used by
        secret-login (the validator at settings.py:331 still rejects empty
        allowlists in real production loads).
        """
        return load_config(allow_stub_telegram=True).telegram.allowed_user_ids

    @staticmethod
    def is_user_allowed(user_id: int, *, fail_open_when_empty: bool = False) -> bool:
        """Check if user_id is in the ALLOWED_USER_IDS whitelist.

        When the whitelist is empty, behavior is governed by fail_open_when_empty:
        - True: allow all users (permissive — suitable for optional whitelist paths)
        - False: deny all users (strict — suitable for high-security auth paths)
        """
        allowed_ids = Config.get_allowed_user_ids()
        if not allowed_ids:
            return fail_open_when_empty
        return user_id in allowed_ids

    @staticmethod
    def get_allowed_client_ids() -> tuple[str, ...]:
        """Get list of allowed client application IDs.

        Client IDs are arbitrary strings that identify specific client
        applications (e.g., "android-app-v1.0", "ios-app-v2.0"). Only clients
        with these IDs can authenticate and receive access tokens. Empty tuple
        = no client restriction only outside production or when
        AUTH_ALLOW_ANY_CLIENT_ID=true is explicitly set.

        Delegates to AuthConfig.allowed_client_ids; the env-var parsing /
        validation lives there as a field validator.
        """
        return load_config(allow_stub_telegram=True).auth.allowed_client_ids

    @staticmethod
    def allow_any_client_id() -> bool:
        """Return whether broad client-ID access is explicitly enabled."""
        return load_config(allow_stub_telegram=True).auth.allow_any_client_id


# Public alias used across the codebase.
Config = ConfigHelper
