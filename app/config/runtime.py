from __future__ import annotations

from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
)

from app.config._secret_marker import SECRET_MARKER
from app.config.validation_helpers import parse_positive_int
from app.core.logging_utils import get_logger

logger = get_logger(__name__)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    db_path: str = Field(default="/data/ratatoskr.db", validation_alias="DB_PATH")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    request_timeout_sec: int = Field(default=60, validation_alias="REQUEST_TIMEOUT_SEC")
    preferred_lang: str = Field(default="auto", validation_alias="PREFERRED_LANG")
    debug_payloads: bool = Field(default=False, validation_alias="DEBUG_PAYLOADS")
    enable_textacy: bool = Field(default=False, validation_alias="TEXTACY_ENABLED")
    enable_chunking: bool = Field(default=True, validation_alias="CHUNKING_ENABLED")
    chunk_max_chars: int = Field(default=200000, validation_alias="CHUNK_MAX_CHARS")
    log_truncate_length: int = Field(default=1000, validation_alias="LOG_TRUNCATE_LENGTH")
    topic_search_max_results: int = Field(default=5, validation_alias="TOPIC_SEARCH_MAX_RESULTS")
    max_concurrent_calls: int = Field(default=4, validation_alias="MAX_CONCURRENT_CALLS")
    summary_prompt_version: str = Field(default="v1", validation_alias="SUMMARY_PROMPT_VERSION")
    summary_streaming_enabled: bool = Field(
        default=True, validation_alias="SUMMARY_STREAMING_ENABLED"
    )
    summary_streaming_mode: str = Field(
        default="section", validation_alias="SUMMARY_STREAMING_MODE"
    )
    summary_streaming_provider_scope: str = Field(
        default="openrouter", validation_alias="SUMMARY_STREAMING_PROVIDER_SCOPE"
    )
    jwt_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices("JWT_SECRET_KEY", "JWT_SECRET"),
        json_schema_extra=SECRET_MARKER,
    )
    db_backup_enabled: bool = Field(default=True, validation_alias="DB_BACKUP_ENABLED")
    db_backup_interval_minutes: int = Field(
        default=360, validation_alias="DB_BACKUP_INTERVAL_MINUTES"
    )
    db_backup_retention: int = Field(default=14, validation_alias="DB_BACKUP_RETENTION")
    db_backup_dir: str | None = Field(default=None, validation_alias="DB_BACKUP_DIR")
    llm_provider: str = Field(default="openrouter", validation_alias="LLM_PROVIDER")
    telegram_reply_timeout_sec: float = Field(
        default=30.0, validation_alias="TELEGRAM_REPLY_TIMEOUT_SEC"
    )
    semaphore_acquire_timeout_sec: float = Field(
        default=30.0, validation_alias="SEMAPHORE_ACQUIRE_TIMEOUT_SEC"
    )
    llm_call_timeout_sec: float = Field(default=420.0, validation_alias="LLM_CALL_TIMEOUT_SEC")
    llm_per_model_timeout_min_sec: float = Field(
        default=90.0, validation_alias="LLM_PER_MODEL_TIMEOUT_MIN_SEC"
    )
    llm_per_model_timeout_overrides: dict[str, float] = Field(
        default_factory=dict, validation_alias="LLM_PER_MODEL_TIMEOUT_OVERRIDES"
    )
    dedupe_retry_grace_sec: float = Field(
        default=60.0, validation_alias="RUNTIME_DEDUPE_RETRY_GRACE_SEC"
    )
    llm_call_max_retries: int = Field(default=2, validation_alias="LLM_CALL_MAX_RETRIES")
    llm_max_calls_per_request: int = Field(
        default=8,
        ge=1,
        validation_alias="LLM_MAX_CALLS_PER_REQUEST",
        description=(
            "Hard ceiling on the number of LLM provider invocations a single "
            "request may make across the attempt list, JSON-repair passes, and "
            "instructor sticky-fallback retries. Each invocation may still try "
            "the configured fallback cascade, so the realistic happy path is "
            "1-2; this cap only bounds the degraded-provider tail. Raise only if "
            "a legitimate multi-attempt flow needs more cascade runs."
        ),
    )
    url_flow_lease_ttl_sec: int = Field(
        default=900,
        ge=60,
        le=3600,
        validation_alias="URL_FLOW_LEASE_TTL_SEC",
        description=(
            "Lease TTL for the request_processing_jobs row created at URL "
            "flow entry on the bot path. The worker's reconcile_stuck_"
            "processing_requests reaps rows whose lease expired without a "
            "terminal status, providing crash-recovery for synchronous bot "
            "runs. Size this above the longest expected URL flow runtime."
        ),
    )
    llm_request_slow_threshold_sec: float = Field(
        default=300.0,
        ge=1.0,
        validation_alias="LLM_REQUEST_SLOW_THRESHOLD_SEC",
        description=(
            "Wall-time threshold above which a URL request is considered slow. "
            "Crossing this triggers a 'url_flow_slow_request' warning log and "
            "increments ratatoskr_llm_request_slow_total. Sized to flag "
            "pathological LLM cascades (12+ min Habr-vision incident) without "
            "firing on legitimate long extracts."
        ),
    )
    llm_budget_tight_ratio: float = Field(
        default=0.6,
        gt=0.0,
        le=1.0,
        validation_alias="LLM_BUDGET_TIGHT_RATIO",
        description=(
            "Fraction of per-model timeout budget at which truncation recovery "
            "is skipped (Budget-Tight Guard in chat_attempt_runner.py). On hosts "
            "where the budget trips on every VL attempt, lower this to give "
            "recovery a chance to run."
        ),
    )
    llm_truncation_max_count: int = Field(
        default=2,
        ge=1,
        validation_alias="LLM_TRUNCATION_MAX_COUNT",
        description=(
            "Maximum consecutive truncated completions from the same model "
            "before the cascade falls through to the next fallback. Lower "
            "values fail-fast on hostile model behaviour; raise on hosts "
            "where genuine retry is worth the wall-time."
        ),
    )
    summarization_max_retries: int = Field(
        default=3,
        ge=1,
        le=10,
        validation_alias="SUMMARIZATION_MAX_RETRIES",
        description=(
            "Max attempts for the instructor self-correction loop. Each retry "
            "re-runs the full LLM call cascade for one summary, so lowering this is the "
            "main lever for cutting total LLM cost on validation-failing summaries."
        ),
    )
    llm_sticky_failure_force_fallback: bool = Field(
        default=True,
        validation_alias="LLM_STICKY_FAILURE_FORCE_FALLBACK",
        description=(
            "When the first chat_structured attempt fails with a sticky error "
            "(per_model_timeout, repeated_truncation, "
            "truncation_recovery_skipped_budget_tight), drop the model_override "
            "and retry once so the cascade picks a different primary. Default on; "
            "set false to preserve the legacy single-attempt behaviour."
        ),
    )
    json_parse_timeout_sec: float = Field(default=60.0, validation_alias="JSON_PARSE_TIMEOUT_SEC")
    summary_two_pass_enabled: bool = Field(
        default=False, validation_alias="SUMMARY_TWO_PASS_ENABLED"
    )
    summarize_rag_enabled: bool = Field(
        default=False,
        validation_alias="SUMMARIZE_RAG_ENABLED",
        description=(
            "Enable RAG grounding in the summarize graph's ground node: retrieve "
            "top-k scope-filtered prior summaries and inject an anti-contamination "
            "'related prior summaries (reference only)' block into the system prompt "
            "(ADR-0005/0012/0016). TRANSITIONAL/opt-in: default off; REMOVE at the T6 "
            "cutover once grounding is the default (no flag outlives its migration, "
            "ADR-0018)."
        ),
    )
    rag_top_k: int = Field(
        default=5,
        validation_alias="RAG_TOP_K",
        description=(
            "Number of prior summaries the ground node retrieves when "
            "SUMMARIZE_RAG_ENABLED is on. REMOVE alongside SUMMARIZE_RAG_ENABLED at "
            "the T6 cutover (ADR-0018)."
        ),
    )
    aggregation_bundle_enabled: bool = Field(
        default=True, validation_alias="AGGREGATION_BUNDLE_ENABLED"
    )
    aggregation_rollout_stage: str = Field(
        default="enabled", validation_alias="AGGREGATION_ROLLOUT_STAGE"
    )
    aggregation_meta_extractors_enabled: bool = Field(
        default=True, validation_alias="AGGREGATION_META_EXTRACTORS_ENABLED"
    )
    aggregation_article_media_enabled: bool = Field(
        default=True, validation_alias="AGGREGATION_ARTICLE_MEDIA_ENABLED"
    )
    aggregation_non_youtube_video_enabled: bool = Field(
        default=True, validation_alias="AGGREGATION_NON_YOUTUBE_VIDEO_ENABLED"
    )
    aggregation_default_mode: str = Field(
        default="per_url", validation_alias="AGGREGATION_DEFAULT_MODE"
    )
    aggregate_coalesce_enabled: bool = Field(
        default=True, validation_alias="AGGREGATE_COALESCE_ENABLED"
    )
    aggregate_coalesce_window_sec: float = Field(
        default=5.0, validation_alias="AGGREGATE_COALESCE_WINDOW_SEC"
    )
    rate_limit_max_requests: int = Field(default=10, validation_alias="RATE_LIMIT_MAX_REQUESTS")
    rate_limit_window_seconds: int = Field(default=60, validation_alias="RATE_LIMIT_WINDOW_SECONDS")
    rate_limit_max_concurrent: int = Field(default=3, validation_alias="RATE_LIMIT_MAX_CONCURRENT")
    related_reads_enabled: bool = Field(default=True, validation_alias="RELATED_READS_ENABLED")
    related_reads_min_similarity: float = Field(
        default=0.75, validation_alias="RELATED_READS_MIN_SIMILARITY"
    )
    url_flow_streaming_enabled: bool = Field(
        default=True, validation_alias="URL_FLOW_STREAMING_ENABLED"
    )
    # Forwarded-post link enrichment: fetch the full content of links embedded
    # in a forwarded channel post and fold it into the post's summary.
    forward_link_max_links: int = Field(default=5, validation_alias="FORWARD_LINK_MAX_LINKS")
    forward_link_per_article_chars: int = Field(
        default=8000, validation_alias="FORWARD_LINK_PER_ARTICLE_CHARS"
    )
    forward_link_per_url_timeout_sec: float = Field(
        default=25.0, validation_alias="FORWARD_LINK_PER_URL_TIMEOUT_SEC"
    )
    forward_link_bundle_prose_threshold: int = Field(
        default=200, validation_alias="FORWARD_LINK_BUNDLE_PROSE_THRESHOLD"
    )
    url_worker_enqueue_enabled: bool = Field(
        default=True,
        validation_alias="URL_WORKER_ENQUEUE_ENABLED",
        description=(
            "When true, the bot hands URL requests to the Taskiq worker "
            "instead of processing them inline. Set to false to fall back "
            "to the synchronous inline path without redeploying."
        ),
    )
    url_worker_concurrency: int = Field(
        default=4,
        ge=1,
        le=16,
        validation_alias="URL_WORKER_CONCURRENCY",
        description=(
            "Maximum number of process_url_request tasks the worker may "
            "execute concurrently.  Wire into Taskiq ``--max-async-tasks`` "
            "at worker startup; see ops/docker/docker-compose.yml."
        ),
    )

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _validate_llm_provider(cls, value: Any) -> str:
        provider = str(value or "openrouter").lower().strip()
        if provider != "openrouter":
            msg = (
                f"Invalid LLM provider: {provider}. Only 'openrouter' is supported. "
                "Use OpenRouter model IDs such as 'openai/...' or 'anthropic/...' "
                "to route to upstream model families."
            )
            raise ValueError(msg)
        return provider

    @field_validator("log_level", mode="before")
    @classmethod
    def _validate_log_level(cls, value: Any) -> str:
        log_level = str(value or "INFO").upper()
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if log_level not in valid_levels:
            msg = f"Invalid log level: {value}. Must be one of {valid_levels}"
            raise ValueError(msg)
        return log_level

    @field_validator("request_timeout_sec", mode="before")
    @classmethod
    def _validate_timeout(cls, value: Any) -> int:
        try:
            timeout = int(str(value or 60))
        except ValueError as exc:  # pragma: no cover - defensive
            msg = "Timeout must be a valid integer"
            raise ValueError(msg) from exc
        if timeout <= 0:
            msg = "Timeout must be positive"
            raise ValueError(msg)
        if timeout > 3600:
            msg = "Timeout too large (max 3600 seconds)"
            raise ValueError(msg)
        return timeout

    @field_validator("preferred_lang", mode="before")
    @classmethod
    def _validate_lang(cls, value: Any) -> str:
        lang = str(value or "auto")
        if lang not in {"auto", "en", "ru"}:
            msg = f"Invalid language: {lang}. Must be one of {{'auto', 'en', 'ru'}}"
            raise ValueError(msg)
        return lang

    @field_validator("chunk_max_chars", "log_truncate_length", mode="before")
    @classmethod
    def _validate_positive_int(cls, value: Any, info: ValidationInfo) -> int:
        default = cls.model_fields[info.field_name].default
        return parse_positive_int(value, field_name=info.field_name, default=default)

    @field_validator(
        "forward_link_max_links",
        "forward_link_per_article_chars",
        "forward_link_bundle_prose_threshold",
        mode="before",
    )
    @classmethod
    def _validate_forward_link_ints(cls, value: Any, info: ValidationInfo) -> int:
        bounds: dict[str, tuple[int, int]] = {
            "forward_link_max_links": (1, 10),
            "forward_link_per_article_chars": (500, 40000),
            "forward_link_bundle_prose_threshold": (0, 10000),
        }
        low, high = bounds[info.field_name]
        default = cls.model_fields[info.field_name].default
        try:
            parsed = int(str(value if value not in (None, "") else default))
        except (ValueError, TypeError) as exc:
            msg = f"{info.field_name} must be a valid integer"
            raise ValueError(msg) from exc
        return max(low, min(high, parsed))

    @field_validator(
        "telegram_reply_timeout_sec",
        "semaphore_acquire_timeout_sec",
        "llm_call_timeout_sec",
        "json_parse_timeout_sec",
        "dedupe_retry_grace_sec",
        "forward_link_per_url_timeout_sec",
        mode="before",
    )
    @classmethod
    def _validate_timeout_float(cls, value: Any, info: ValidationInfo) -> float:
        default = cls.model_fields[info.field_name].default
        try:
            parsed = float(str(value if value not in (None, "") else default))
        except (ValueError, TypeError) as exc:
            msg = f"{info.field_name} must be a valid number"
            raise ValueError(msg) from exc
        if parsed < 0.1 or parsed > 3600.0:
            msg = f"{info.field_name} must be between 0.1 and 3600 seconds, got {parsed}"
            raise ValueError(msg)
        return parsed

    @field_validator("topic_search_max_results", mode="before")
    @classmethod
    def _validate_topic_search_limit(cls, value: Any) -> int:
        default = cls.model_fields["topic_search_max_results"].default
        try:
            parsed = int(str(value if value not in (None, "") else default))
        except ValueError as exc:  # pragma: no cover - defensive
            msg = "Topic search max results must be a valid integer"
            raise ValueError(msg) from exc
        if parsed <= 0:
            msg = "Topic search max results must be positive"
            raise ValueError(msg)
        if parsed > 10:
            msg = "Topic search max results must be 10 or fewer"
            raise ValueError(msg)
        return parsed

    @field_validator("summary_prompt_version", mode="before")
    @classmethod
    def _validate_prompt_version(cls, value: Any) -> str:
        raw = str(value or "v1").strip()
        if not raw:
            msg = "Summary prompt version cannot be empty"
            raise ValueError(msg)
        if len(raw) > 30:
            msg = "Summary prompt version is too long"
            raise ValueError(msg)
        if any(ch.isspace() for ch in raw):
            msg = "Summary prompt version cannot contain whitespace"
            raise ValueError(msg)
        return raw

    @field_validator("summary_streaming_mode", mode="before")
    @classmethod
    def _validate_summary_streaming_mode(cls, value: Any) -> str:
        mode = str(value or "section").strip().lower()
        allowed = {"section", "disabled"}
        if mode not in allowed:
            msg = f"Summary streaming mode must be one of {sorted(allowed)}"
            raise ValueError(msg)
        return mode

    @field_validator("summary_streaming_provider_scope", mode="before")
    @classmethod
    def _validate_summary_streaming_scope(cls, value: Any) -> str:
        scope = str(value or "openrouter").strip().lower()
        allowed = {"openrouter", "all", "disabled"}
        if scope not in allowed:
            msg = f"Summary streaming provider scope must be one of {sorted(allowed)}"
            raise ValueError(msg)
        return scope

    @field_validator("aggregation_rollout_stage", mode="before")
    @classmethod
    def _validate_aggregation_rollout_stage(cls, value: Any) -> str:
        stage = str(value or "enabled").strip().lower()
        allowed = {"disabled", "internal", "owner_beta", "enabled"}
        if stage not in allowed:
            msg = f"Aggregation rollout stage must be one of {sorted(allowed)}"
            raise ValueError(msg)
        return stage

    @field_validator("aggregation_default_mode", mode="before")
    @classmethod
    def _validate_aggregation_default_mode(cls, value: Any) -> str:
        mode = str(value or "per_url").strip().lower()
        allowed = {"per_url", "bundle"}
        if mode not in allowed:
            msg = f"Aggregation default mode must be one of {sorted(allowed)}"
            raise ValueError(msg)
        return mode

    @field_validator("db_backup_interval_minutes", mode="before")
    @classmethod
    def _validate_backup_interval(cls, value: Any) -> int:
        try:
            parsed = int(str(value or 360))
        except ValueError as exc:
            msg = "DB backup interval (minutes) must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 5 or parsed > 10080:
            msg = "DB backup interval (minutes) must be between 5 and 10080"
            raise ValueError(msg)
        return parsed

    @field_validator("db_backup_retention", mode="before")
    @classmethod
    def _validate_backup_retention(cls, value: Any) -> int:
        try:
            parsed = int(str(value or 14))
        except ValueError as exc:
            msg = "DB backup retention must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 0 or parsed > 1000:
            msg = "DB backup retention must be between 0 and 1000"
            raise ValueError(msg)
        return parsed

    @field_validator("db_backup_dir", mode="before")
    @classmethod
    def _validate_backup_dir(cls, value: Any) -> str | None:
        if value is None:
            return None
        trimmed = str(value).strip()
        if not trimmed:
            return None
        if "\x00" in trimmed:
            msg = "DB backup directory contains invalid characters"
            raise ValueError(msg)
        return trimmed

    @field_validator("llm_per_model_timeout_overrides", mode="before")
    @classmethod
    def _validate_llm_per_model_timeout_overrides(cls, value: Any) -> dict[str, float]:
        """Parse comma-separated 'model=seconds' pairs into a dict.

        Accepts either a pre-built dict (e.g. from YAML config) or a raw
        string value from the environment variable.  Malformed entries are
        logged and skipped so a bad value never prevents startup.
        """
        if isinstance(value, dict):
            # Already parsed (e.g. loaded from ratatoskr.yaml).
            result: dict[str, float] = {}
            for k, v in value.items():
                try:
                    result[str(k).strip()] = float(v)
                except (ValueError, TypeError):
                    logger.warning(
                        "llm_per_model_timeout_overrides_bad_entry",
                        extra={"key": k, "value": v},
                    )
            return result
        raw = str(value or "").strip()
        if not raw:
            return {}
        result = {}
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if "=" not in entry:
                logger.warning(
                    "llm_per_model_timeout_overrides_bad_entry",
                    extra={"entry": entry},
                )
                continue
            model_name, _, seconds_str = entry.partition("=")
            model_name = model_name.strip()
            seconds_str = seconds_str.strip()
            if not model_name or not seconds_str:
                logger.warning(
                    "llm_per_model_timeout_overrides_bad_entry",
                    extra={"entry": entry},
                )
                continue
            try:
                result[model_name] = float(seconds_str)
            except ValueError:
                logger.warning(
                    "llm_per_model_timeout_overrides_bad_entry",
                    extra={"entry": entry, "seconds_str": seconds_str},
                )
        return result

    @field_validator("llm_call_max_retries", mode="before")
    @classmethod
    def _validate_llm_call_max_retries(cls, value: Any) -> int:
        try:
            parsed = int(str(value or 2))
        except ValueError as exc:
            msg = "LLM call max retries must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 0 or parsed > 5:
            msg = "LLM call max retries must be between 0 and 5"
            raise ValueError(msg)
        return parsed

    @field_validator("max_concurrent_calls", mode="before")
    @classmethod
    def _validate_max_concurrent_calls(cls, value: Any) -> int:
        try:
            parsed = int(str(value or 4))
        except ValueError as exc:
            msg = "Max concurrent calls must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 1 or parsed > 100:
            msg = "Max concurrent calls must be between 1 and 100"
            raise ValueError(msg)
        return parsed

    @field_validator("rate_limit_max_requests", mode="before")
    @classmethod
    def _validate_rate_limit_max_requests(cls, value: Any) -> int:
        try:
            parsed = int(str(value if value not in (None, "") else 10))
        except ValueError as exc:
            msg = "Rate limit max requests must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 1 or parsed > 100:
            msg = "Rate limit max requests must be between 1 and 100"
            raise ValueError(msg)
        return parsed

    @field_validator("rate_limit_window_seconds", mode="before")
    @classmethod
    def _validate_rate_limit_window_seconds(cls, value: Any) -> int:
        try:
            parsed = int(str(value if value not in (None, "") else 60))
        except ValueError as exc:
            msg = "Rate limit window seconds must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 10 or parsed > 3600:
            msg = "Rate limit window seconds must be between 10 and 3600"
            raise ValueError(msg)
        return parsed

    @field_validator("rate_limit_max_concurrent", mode="before")
    @classmethod
    def _validate_rate_limit_max_concurrent(cls, value: Any) -> int:
        try:
            parsed = int(str(value if value not in (None, "") else 3))
        except ValueError as exc:
            msg = "Rate limit max concurrent must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 1 or parsed > 20:
            msg = "Rate limit max concurrent must be between 1 and 20"
            raise ValueError(msg)
        return parsed

    @field_validator("related_reads_min_similarity", mode="before")
    @classmethod
    def _validate_related_reads_min_similarity(cls, value: Any) -> float:
        default = cls.model_fields["related_reads_min_similarity"].default
        try:
            parsed = float(str(value if value not in (None, "") else default))
        except (ValueError, TypeError) as exc:
            msg = "Related reads min similarity must be a valid number"
            raise ValueError(msg) from exc
        if parsed < 0.0 or parsed > 1.0:
            msg = "Related reads min similarity must be between 0.0 and 1.0"
            raise ValueError(msg)
        return parsed

    @field_validator("jwt_secret_key", mode="before")
    @classmethod
    def _validate_jwt_secret_key(cls, value: Any) -> str:
        if value in (None, ""):
            return ""
        secret = str(value).strip()
        if len(secret) < 32:
            msg = "JWT secret key must be at least 32 characters when provided"
            raise ValueError(msg)
        if len(secret) > 500:
            msg = "JWT secret key appears to be too long"
            raise ValueError(msg)
        return secret
