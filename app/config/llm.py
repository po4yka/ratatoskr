from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.logging_utils import get_logger

from ._secret_marker import SECRET_MARKER
from ._validators import _ensure_api_key, parse_fallback_models, validate_model_name

logger = get_logger(__name__)


class _FallbackModelsMixin:
    @field_validator("fallback_models", mode="before")
    @classmethod
    def _parse_fallback_models(cls, value: Any) -> tuple[str, ...]:
        return parse_fallback_models(value)


class LLMUsageBudgetConfig(BaseModel):
    """Operator-controlled limits for LLM consumption."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    max_tokens_per_request: int | None = Field(
        default=None,
        validation_alias="LLM_MAX_TOKENS_PER_REQUEST",
        description="Maximum prompt+completion tokens allowed for a persisted LLM call; unset disables the check.",
    )
    max_cost_usd_per_request: float | None = Field(
        default=None,
        validation_alias="LLM_MAX_COST_USD_PER_REQUEST",
        description="Maximum estimated USD cost allowed for a persisted LLM call; unset disables the check.",
    )
    daily_soft_budget_usd: float | None = Field(
        default=None,
        validation_alias="LLM_DAILY_SOFT_BUDGET_USD",
        description="Daily warning budget for aggregate LLM cost; unset disables the soft budget.",
    )
    monthly_soft_budget_usd: float | None = Field(
        default=None,
        validation_alias="LLM_MONTHLY_SOFT_BUDGET_USD",
        description="Monthly warning budget for aggregate LLM cost; unset disables the soft budget.",
    )
    warning_threshold_ratio: float = Field(
        default=0.8,
        validation_alias="LLM_BUDGET_WARNING_THRESHOLD_RATIO",
        description="Ratio of a configured soft budget that starts reporting warning status.",
    )
    daily_hard_budget_usd: float | None = Field(
        default=None,
        validation_alias="LLM_DAILY_HARD_BUDGET_USD",
        description="Daily aggregate LLM cost at which new workflow LLM calls are blocked; unset disables hard stop.",
    )
    monthly_hard_budget_usd: float | None = Field(
        default=None,
        validation_alias="LLM_MONTHLY_HARD_BUDGET_USD",
        description="Monthly aggregate LLM cost at which new workflow LLM calls are blocked; unset disables hard stop.",
    )

    @field_validator(
        "max_tokens_per_request",
        mode="before",
    )
    @classmethod
    def _validate_optional_positive_int(cls, value: Any) -> int | None:
        if value in (None, ""):
            return None
        parsed = int(str(value))
        if parsed < 1:
            msg = "LLM_MAX_TOKENS_PER_REQUEST must be at least 1 when set"
            raise ValueError(msg)
        return parsed

    @field_validator(
        "max_cost_usd_per_request",
        "daily_soft_budget_usd",
        "monthly_soft_budget_usd",
        "daily_hard_budget_usd",
        "monthly_hard_budget_usd",
        mode="before",
    )
    @classmethod
    def _validate_optional_non_negative_float(cls, value: Any) -> float | None:
        if value in (None, ""):
            return None
        parsed = float(str(value))
        if parsed < 0:
            msg = "LLM budget values must be non-negative"
            raise ValueError(msg)
        return parsed

    @field_validator("warning_threshold_ratio", mode="before")
    @classmethod
    def _validate_warning_threshold(cls, value: Any) -> float:
        if value in (None, ""):
            return 0.8
        parsed = float(str(value))
        if parsed <= 0 or parsed > 1:
            msg = "LLM_BUDGET_WARNING_THRESHOLD_RATIO must be greater than 0 and at most 1"
            raise ValueError(msg)
        return parsed


class DirectOpenAIConfig(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    api_key: str | None = Field(
        default=None, validation_alias="OPENAI_API_KEY", json_schema_extra=SECRET_MARKER
    )
    model: str | None = Field(default=None, validation_alias="OPENAI_MODEL")
    base_url: str = Field(default="https://api.openai.com/v1", validation_alias="OPENAI_BASE_URL")
    max_tokens: int | None = Field(default=None, validation_alias="OPENAI_MAX_TOKENS")
    temperature: float = Field(default=0.2, validation_alias="OPENAI_TEMPERATURE")
    timeout_sec: int = Field(default=60, validation_alias="OPENAI_TIMEOUT_SEC")
    max_retries: int = Field(default=3, validation_alias="OPENAI_MAX_RETRIES")
    max_response_size_mb: int = Field(default=10, validation_alias="OPENAI_MAX_RESPONSE_SIZE_MB")

    @field_validator("api_key", mode="before")
    @classmethod
    def _normalize_api_key(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("model", mode="before")
    @classmethod
    def _validate_model(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return validate_model_name(str(value))


class DirectAnthropicConfig(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    api_key: str | None = Field(
        default=None, validation_alias="ANTHROPIC_API_KEY", json_schema_extra=SECRET_MARKER
    )
    model: str | None = Field(default=None, validation_alias="ANTHROPIC_MODEL")
    base_url: str = Field(
        default="https://api.anthropic.com/v1", validation_alias="ANTHROPIC_BASE_URL"
    )
    version: str = Field(default="2023-06-01", validation_alias="ANTHROPIC_VERSION")
    max_tokens: int = Field(default=4096, validation_alias="ANTHROPIC_MAX_TOKENS")
    temperature: float = Field(default=0.2, validation_alias="ANTHROPIC_TEMPERATURE")
    timeout_sec: int = Field(default=60, validation_alias="ANTHROPIC_TIMEOUT_SEC")
    max_retries: int = Field(default=3, validation_alias="ANTHROPIC_MAX_RETRIES")
    max_response_size_mb: int = Field(default=10, validation_alias="ANTHROPIC_MAX_RESPONSE_SIZE_MB")

    @field_validator("api_key", mode="before")
    @classmethod
    def _normalize_api_key(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("model", mode="before")
    @classmethod
    def _validate_model(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value).strip()


class DirectOllamaConfig(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    api_key: str | None = Field(
        default=None, validation_alias="OLLAMA_API_KEY", json_schema_extra=SECRET_MARKER
    )
    model: str | None = Field(default=None, validation_alias="OLLAMA_MODEL")
    base_url: str = Field(default="http://localhost:11434/v1", validation_alias="OLLAMA_BASE_URL")
    max_tokens: int | None = Field(default=None, validation_alias="OLLAMA_MAX_TOKENS")
    temperature: float = Field(default=0.2, validation_alias="OLLAMA_TEMPERATURE")
    timeout_sec: int = Field(default=120, validation_alias="OLLAMA_TIMEOUT_SEC")
    max_retries: int = Field(default=1, validation_alias="OLLAMA_MAX_RETRIES")
    max_response_size_mb: int = Field(default=10, validation_alias="OLLAMA_MAX_RESPONSE_SIZE_MB")

    @field_validator("api_key", mode="before")
    @classmethod
    def _normalize_api_key(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("model", mode="before")
    @classmethod
    def _validate_model(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value).strip()


class OpenRouterConfig(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    api_key: str = Field(
        ..., validation_alias="OPENROUTER_API_KEY", json_schema_extra=SECRET_MARKER
    )
    # Model selection is intentionally default-free: every value must be supplied
    # by ratatoskr.yaml (or an env override). The bot hard-fails at startup if a
    # model field is absent, so the YAML file is the single source of truth for
    # which models the service uses. See docs/reference/environment-variables.md.
    model: str = Field(validation_alias="OPENROUTER_MODEL")
    # Order: fastest to most-capable; fastest-first ensures the user gets a response
    # before the outer per-URL timeout fires when the primary stalls.
    fallback_models: tuple[str, ...] = Field(
        validation_alias="OPENROUTER_FALLBACK_MODELS",
    )
    http_referer: str | None = Field(default=None, validation_alias="OPENROUTER_HTTP_REFERER")
    x_title: str | None = Field(default=None, validation_alias="OPENROUTER_X_TITLE")
    max_tokens: int | None = Field(default=None, validation_alias="OPENROUTER_MAX_TOKENS")
    per_model_max_tokens_overrides: dict[str, int] = Field(
        default_factory=dict,
        validation_alias="OPENROUTER_PER_MODEL_MAX_TOKENS_OVERRIDES",
        description=(
            "Per-model completion-budget clamp. Comma-separated 'model=tokens' pairs, "
            "e.g. 'qwen/qwen3-vl-32b-instruct=3072'. Lowers (never raises) the per-call "
            "max_tokens for the named model so models with tight output budgets don't "
            "burn the entire per-model timeout producing a truncated response. Malformed "
            "entries are logged and skipped; an empty value disables the override."
        ),
    )
    top_p: float | None = Field(default=None, validation_alias="OPENROUTER_TOP_P")
    temperature: float = Field(validation_alias="OPENROUTER_TEMPERATURE")
    provider_order: tuple[str, ...] = Field(
        default_factory=tuple, validation_alias="OPENROUTER_PROVIDER_ORDER"
    )
    enable_stats: bool = Field(validation_alias="OPENROUTER_ENABLE_STATS")
    long_context_model: str | None = Field(
        validation_alias="OPENROUTER_LONG_CONTEXT_MODEL",
    )
    flash_model: str = Field(validation_alias="OPENROUTER_FLASH_MODEL")
    flash_fallback_models: tuple[str, ...] = Field(
        validation_alias="OPENROUTER_FLASH_FALLBACK_MODELS",
    )
    summary_temperature_relaxed: float | None = Field(
        default=None, validation_alias="OPENROUTER_SUMMARY_TEMPERATURE_RELAXED"
    )
    summary_top_p_relaxed: float | None = Field(
        default=None, validation_alias="OPENROUTER_SUMMARY_TOP_P_RELAXED"
    )
    summary_temperature_json_fallback: float | None = Field(
        default=None, validation_alias="OPENROUTER_SUMMARY_TEMPERATURE_JSON"
    )
    summary_top_p_json_fallback: float | None = Field(
        default=None, validation_alias="OPENROUTER_SUMMARY_TOP_P_JSON"
    )
    enable_structured_outputs: bool = Field(validation_alias="OPENROUTER_ENABLE_STRUCTURED_OUTPUTS")
    structured_output_mode: str = Field(validation_alias="OPENROUTER_STRUCTURED_OUTPUT_MODE")
    require_parameters: bool = Field(validation_alias="OPENROUTER_REQUIRE_PARAMETERS")
    auto_fallback_structured: bool = Field(validation_alias="OPENROUTER_AUTO_FALLBACK_STRUCTURED")
    max_response_size_mb: int = Field(validation_alias="OPENROUTER_MAX_RESPONSE_SIZE_MB")
    # Prompt caching settings (reduces inference costs)
    enable_prompt_caching: bool = Field(
        validation_alias="OPENROUTER_ENABLE_PROMPT_CACHING",
        description="Enable OpenRouter prompt caching for supported providers",
    )
    prompt_cache_ttl: str = Field(
        validation_alias="OPENROUTER_PROMPT_CACHE_TTL",
        description="Cache TTL for non-Anthropic explicit-cache providers (Google): 'ephemeral' (5min) or '1h'",
    )
    prompt_cache_ttl_anthropic: str = Field(
        validation_alias="OPENROUTER_PROMPT_CACHE_TTL_ANTHROPIC",
        description=(
            "Cache TTL for Anthropic models: 'ephemeral' (1.25x write, 0.10x read) "
            "or '1h' (2x write, 0.10x read). Defaults to '1h' since the longer TTL "
            "amortizes positively across batched summarization requests."
        ),
    )
    cache_system_prompt: bool = Field(
        validation_alias="OPENROUTER_CACHE_SYSTEM_PROMPT",
        description="Cache the system message for reuse across requests",
    )
    cache_large_content_threshold: int = Field(
        validation_alias="OPENROUTER_CACHE_LARGE_CONTENT_THRESHOLD",
        description="Minimum tokens to auto-cache content (Gemini requires 4096)",
    )
    # Transport-layer retry settings (tenacity, network errors only)
    transport_retry_max_attempts: int = Field(
        validation_alias="OPENROUTER_TRANSPORT_RETRY_MAX_ATTEMPTS",
        description="Max attempts for network-class transport errors (httpx connection/timeout)",
    )
    transport_retry_min_wait_sec: float = Field(
        validation_alias="OPENROUTER_TRANSPORT_RETRY_MIN_WAIT_SEC",
        description="Initial wait (seconds) before first transport retry",
    )
    transport_retry_max_wait_sec: float = Field(
        validation_alias="OPENROUTER_TRANSPORT_RETRY_MAX_WAIT_SEC",
        description="Maximum wait (seconds) between transport retries",
    )

    @field_validator("api_key", mode="before")
    @classmethod
    def _validate_api_key(cls, value: Any) -> str:
        return _ensure_api_key(str(value or ""), name="OpenRouter")

    @field_validator("model", mode="before")
    @classmethod
    def _validate_model(cls, value: Any) -> str:
        return validate_model_name(str(value or ""))

    @field_validator("fallback_models", "flash_fallback_models", mode="before")
    @classmethod
    def _parse_fallback_models(cls, value: Any) -> tuple[str, ...]:
        return parse_fallback_models(value)

    @field_validator("long_context_model", "flash_model", mode="before")
    @classmethod
    def _validate_long_context_model(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return validate_model_name(str(value))

    @field_validator("max_tokens", mode="before")
    @classmethod
    def _validate_max_tokens(cls, value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            tokens = int(str(value))
        except ValueError as exc:
            msg = "Max tokens must be a valid integer"
            raise ValueError(msg) from exc
        if tokens <= 0:
            msg = "Max tokens must be positive"
            raise ValueError(msg)
        if tokens > 100000:
            msg = "Max tokens too large"
            raise ValueError(msg)
        return tokens

    @field_validator("per_model_max_tokens_overrides", mode="before")
    @classmethod
    def _validate_per_model_max_tokens_overrides(cls, value: Any) -> dict[str, int]:
        """Parse comma-separated ``model=tokens`` pairs into a dict.

        Mirrors ``RuntimeConfig._validate_llm_per_model_timeout_overrides``: accepts
        either a pre-built dict (YAML config) or a raw env-var string. Malformed
        entries are logged and skipped so a bad value never prevents startup.
        """
        if isinstance(value, dict):
            result: dict[str, int] = {}
            for k, v in value.items():
                try:
                    parsed = int(v)
                except (TypeError, ValueError):
                    logger.warning(
                        "openrouter_per_model_max_tokens_overrides_bad_entry",
                        extra={"key": k, "value": v},
                    )
                    continue
                if parsed <= 0:
                    logger.warning(
                        "openrouter_per_model_max_tokens_overrides_bad_entry",
                        extra={"key": k, "value": v, "reason": "non_positive"},
                    )
                    continue
                result[str(k).strip()] = parsed
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
                    "openrouter_per_model_max_tokens_overrides_bad_entry",
                    extra={"entry": entry},
                )
                continue
            model_name, _, tokens_str = entry.partition("=")
            model_name = model_name.strip()
            tokens_str = tokens_str.strip()
            if not model_name or not tokens_str:
                logger.warning(
                    "openrouter_per_model_max_tokens_overrides_bad_entry",
                    extra={"entry": entry},
                )
                continue
            try:
                parsed = int(tokens_str)
            except ValueError:
                logger.warning(
                    "openrouter_per_model_max_tokens_overrides_bad_entry",
                    extra={"entry": entry, "tokens_str": tokens_str},
                )
                continue
            if parsed <= 0:
                logger.warning(
                    "openrouter_per_model_max_tokens_overrides_bad_entry",
                    extra={"entry": entry, "reason": "non_positive"},
                )
                continue
            result[model_name] = parsed
        return result

    @field_validator("top_p", mode="before")
    @classmethod
    def _validate_top_p(cls, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            top_p = float(str(value))
        except ValueError as exc:
            msg = "Top_p must be a valid number"
            raise ValueError(msg) from exc
        if top_p < 0 or top_p > 1:
            msg = "Top_p must be between 0 and 1"
            raise ValueError(msg)
        return top_p

    @field_validator("summary_top_p_relaxed", "summary_top_p_json_fallback", mode="before")
    @classmethod
    def _validate_summary_top_p(cls, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            parsed = float(str(value))
        except ValueError as exc:
            msg = "Summary top_p override must be a valid number"
            raise ValueError(msg) from exc
        if parsed < 0 or parsed > 1:
            msg = "Summary top_p override must be between 0 and 1"
            raise ValueError(msg)
        return parsed

    @field_validator("temperature", mode="before")
    @classmethod
    def _validate_temperature(cls, value: Any) -> float:
        if value in (None, ""):
            msg = "temperature is required (no code default); set it in ratatoskr.yaml"
            raise ValueError(msg)
        try:
            temperature = float(str(value))
        except ValueError as exc:
            msg = "Temperature must be a valid number"
            raise ValueError(msg) from exc
        if temperature < 0 or temperature > 2:
            msg = "Temperature must be between 0 and 2"
            raise ValueError(msg)
        return temperature

    @field_validator(
        "summary_temperature_relaxed",
        "summary_temperature_json_fallback",
        mode="before",
    )
    @classmethod
    def _validate_summary_temperatures(cls, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            parsed = float(str(value))
        except ValueError as exc:
            msg = "Summary temperature override must be a valid number"
            raise ValueError(msg) from exc
        if parsed < 0 or parsed > 2:
            msg = "Summary temperature override must be between 0 and 2"
            raise ValueError(msg)
        return parsed

    @field_validator("structured_output_mode", mode="before")
    @classmethod
    def _validate_structured_output_mode(cls, value: Any) -> str:
        if value in (None, ""):
            msg = "structured_output_mode is required (no code default); set it in ratatoskr.yaml"
            raise ValueError(msg)
        mode_value = str(value)
        if mode_value not in {"json_schema", "json_object"}:
            msg = f"Invalid structured output mode: {mode_value}. Must be one of {{'json_schema', 'json_object'}}"
            raise ValueError(msg)
        return mode_value

    @field_validator("provider_order", mode="before")
    @classmethod
    def _parse_provider_order(cls, value: Any) -> tuple[str, ...]:
        if value in (None, ""):
            return ()
        iterable = value if isinstance(value, list | tuple) else str(value).split(",")

        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-:")
        parsed: list[str] = []
        for raw in iterable:
            slug = str(raw).strip()
            if not slug or len(slug) > 100:
                continue
            if any(ch not in allowed for ch in slug):
                continue
            parsed.append(slug)
        return tuple(parsed)

    @field_validator("max_response_size_mb", mode="before")
    @classmethod
    def _validate_max_response_size_mb(cls, value: Any) -> int:
        if value in (None, ""):
            msg = "max_response_size_mb is required (no code default); set it in ratatoskr.yaml"
            raise ValueError(msg)
        try:
            size_mb = int(str(value))
        except ValueError as exc:
            msg = "Max response size must be a valid integer"
            raise ValueError(msg) from exc
        if size_mb < 1 or size_mb > 100:
            msg = "Max response size must be between 1 and 100 MB"
            raise ValueError(msg)
        return size_mb

    @field_validator("prompt_cache_ttl", "prompt_cache_ttl_anthropic", mode="before")
    @classmethod
    def _validate_prompt_cache_ttl(cls, value: Any) -> str:
        if value in (None, ""):
            msg = "prompt_cache_ttl is required (no code default); set it in ratatoskr.yaml"
            raise ValueError(msg)
        ttl = str(value).strip().lower()
        if ttl not in {"ephemeral", "1h"}:
            msg = f"Invalid prompt cache TTL: {ttl}. Must be 'ephemeral' or '1h'"
            raise ValueError(msg)
        return ttl

    @field_validator("cache_large_content_threshold", mode="before")
    @classmethod
    def _validate_cache_large_content_threshold(cls, value: Any) -> int:
        if value in (None, ""):
            msg = "cache_large_content_threshold is required (no code default); set it in ratatoskr.yaml"
            raise ValueError(msg)
        try:
            threshold = int(str(value))
        except ValueError as exc:
            msg = "Cache large content threshold must be a valid integer"
            raise ValueError(msg) from exc
        if threshold < 0 or threshold > 100000:
            msg = "Cache large content threshold must be between 0 and 100000"
            raise ValueError(msg)
        return threshold


class ModelRoutingConfig(BaseModel):
    """Content-aware model routing configuration.

    Routes content to different models based on detected content tier
    (technical, sociopolitical, default) using lightweight heuristics.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(default=False, validation_alias="MODEL_ROUTING_ENABLED")
    default_model: str = Field(
        default="deepseek/deepseek-v4-flash",
        validation_alias="MODEL_ROUTING_DEFAULT",
    )
    technical_model: str = Field(
        default="deepseek/deepseek-v4-pro",
        validation_alias="MODEL_ROUTING_TECHNICAL",
    )
    sociopolitical_model: str = Field(
        default="x-ai/grok-4.20-beta",
        validation_alias="MODEL_ROUTING_SOCIOPOLITICAL",
    )
    long_context_model: str = Field(
        default="qwen/qwen3.6-plus-04-02",
        validation_alias="MODEL_ROUTING_LONG_CONTEXT",
    )
    fallback_models: tuple[str, ...] = Field(
        default_factory=lambda: (
            "deepseek/deepseek-v4-flash",
            "qwen/qwen3.6-plus-04-02",
            "minimax/minimax-m2",
        ),
        validation_alias="MODEL_ROUTING_FALLBACK_MODELS",
    )
    technical_fallback_models: tuple[str, ...] = Field(
        default_factory=tuple,
        validation_alias="MODEL_ROUTING_TECHNICAL_FALLBACK_MODELS",
    )
    sociopolitical_fallback_models: tuple[str, ...] = Field(
        default_factory=tuple,
        validation_alias="MODEL_ROUTING_SOCIOPOLITICAL_FALLBACK_MODELS",
    )
    default_fallback_models: tuple[str, ...] = Field(
        default_factory=tuple,
        validation_alias="MODEL_ROUTING_DEFAULT_FALLBACK_MODELS",
    )
    vision_model: str | None = Field(
        default=None,
        validation_alias="MODEL_ROUTING_VISION",
    )
    quick_model: str | None = Field(
        default=None,
        validation_alias="MODEL_ROUTING_QUICK",
    )
    quick_threshold_tokens: int = Field(
        default=500,
        validation_alias="MODEL_ROUTING_QUICK_THRESHOLD_TOKENS",
    )
    long_context_threshold_tokens: int = Field(
        default=180000,
        validation_alias="MODEL_ROUTING_LONG_CONTEXT_THRESHOLD_TOKENS",
    )
    llm_classifier_enabled: bool = Field(
        default=False,
        validation_alias="LLM_CLASSIFIER_ENABLED",
        description=(
            "Opt-in: consult a cheap LLM to break heuristic tier ties "
            "(tech_weight == socio_weight >= 1). Default False."
        ),
    )
    llm_classifier_model: str = Field(
        default="qwen/qwen3.6-flash",
        validation_alias="LLM_CLASSIFIER_MODEL",
        description=(
            "Model used by the tie-breaking classifier when enabled. "
            "Must be a fast, cheap model — single-word reply, ~100 tokens."
        ),
    )
    latency_stickiness_factor: float = Field(
        default=2.0,
        validation_alias="MODEL_ROUTING_LATENCY_STICKINESS_FACTOR",
        description=(
            "Re-order fallback chain only when a candidate's P95 latency is "
            "at least this factor smaller than the next configured model. "
            "Prevents thrashing when latencies are similar. Default 2.0x."
        ),
    )
    latency_cache_ttl_seconds: float = Field(
        default=300.0,
        validation_alias="MODEL_ROUTING_LATENCY_CACHE_TTL_SECONDS",
        description=(
            "How long the fallback orderer caches per-model P95 latency "
            "before consulting the latency stats repository again."
        ),
    )
    max_escalations: int = Field(
        default=2,
        validation_alias="MODEL_ROUTING_MAX_ESCALATIONS",
        description=(
            "Cap on soft-failure escalations per request. A 'soft failure' "
            "is a 200 response that cannot be parsed as JSON or fails "
            "strict-contract validation. Each escalation advances to the "
            "next model in the fallback chain instead of re-spending the "
            "retry budget on the same model. Set to 0 to disable."
        ),
    )
    max_provider_rotations: int = Field(
        default=2,
        validation_alias="MODEL_ROUTING_MAX_PROVIDER_ROTATIONS",
        description=(
            "On a provider-specific rejection (OpenRouter "
            "metadata.provider_name set), retry the same model up to this "
            "many times with an updated provider_order header excluding "
            "the offending provider before advancing to the next model. "
            "Set to 0 to keep the legacy 'advance model immediately' behaviour."
        ),
    )

    @field_validator(
        "default_model",
        "technical_model",
        "sociopolitical_model",
        "long_context_model",
        mode="before",
    )
    @classmethod
    def _validate_model(cls, value: Any) -> str:
        if value in (None, ""):
            return str(value or "")
        return validate_model_name(str(value))

    @field_validator(
        "fallback_models",
        "technical_fallback_models",
        "sociopolitical_fallback_models",
        "default_fallback_models",
        mode="before",
    )
    @classmethod
    def _parse_fallback_models(cls, value: Any) -> tuple[str, ...]:
        return parse_fallback_models(value)

    @field_validator("vision_model", "quick_model", mode="before")
    @classmethod
    def _validate_optional_model(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return validate_model_name(str(value))

    @field_validator("quick_threshold_tokens", mode="before")
    @classmethod
    def _validate_quick_threshold(cls, value: Any) -> int:
        if value in (None, ""):
            return 500
        try:
            threshold = int(str(value))
        except ValueError as exc:
            msg = "Quick threshold must be a valid integer"
            raise ValueError(msg) from exc
        if threshold < 1:
            msg = "Quick threshold must be at least 1"
            raise ValueError(msg)
        return threshold

    @field_validator("long_context_threshold_tokens", mode="before")
    @classmethod
    def _validate_threshold(cls, value: Any) -> int:
        if value in (None, ""):
            return 180000
        try:
            threshold = int(str(value))
        except ValueError as exc:
            msg = "Long context threshold must be a valid integer"
            raise ValueError(msg) from exc
        if threshold < 1000:
            msg = "Long context threshold must be at least 1000"
            raise ValueError(msg)
        return threshold
