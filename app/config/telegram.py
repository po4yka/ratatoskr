from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from ._secret_marker import SECRET_MARKER
from ._validators import _ensure_api_key, _parse_allowed_user_ids
from .validation_helpers import parse_positive_int


class TelegramConfig(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    api_id: int = Field(
        ...,
        validation_alias=AliasChoices("API_ID", "TELEGRAM_API_ID"),
        description="Telegram API ID",
        json_schema_extra=SECRET_MARKER,
    )
    api_hash: str = Field(
        ...,
        validation_alias=AliasChoices("API_HASH", "TELEGRAM_API_HASH"),
        description="Telegram API hash",
        json_schema_extra=SECRET_MARKER,
    )
    bot_token: str = Field(
        ...,
        validation_alias=AliasChoices("BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
        description="Telegram bot token",
        json_schema_extra=SECRET_MARKER,
    )
    allowed_user_ids: tuple[int, ...] = Field(
        default_factory=tuple,
        validation_alias=AliasChoices("ALLOWED_USER_IDS", "TELEGRAM_ALLOWED_USER_IDS"),
        description="Comma separated list of Telegram user IDs that may interact with the bot",
        json_schema_extra=SECRET_MARKER,
    )
    admin_log_chat_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("ADMIN_LOG_CHAT_ID", "TELEGRAM_ADMIN_LOG_CHAT_ID"),
        description="Chat ID to send debug-level notifications to (optional)",
    )
    forum_topics_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("FORUM_TOPICS_ENABLED", "TELEGRAM_FORUM_TOPICS_ENABLED"),
        description="Enable forum topics in private chats to organize summaries by category",
    )
    api_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("API_BASE_URL", "TELEGRAM_API_BASE_URL"),
        description="Public HTTPS base URL for the FastAPI instance (used for Mini App URLs)",
    )
    draft_streaming_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TELEGRAM_DRAFT_STREAMING_ENABLED", "DRAFT_STREAMING_ENABLED"
        ),
        description="Enable Telegram draft streaming via sendMessageDraft",
    )
    draft_min_interval_ms: int = Field(
        default=700,
        validation_alias=AliasChoices("TELEGRAM_DRAFT_MIN_INTERVAL_MS", "DRAFT_MIN_INTERVAL_MS"),
        description="Minimum interval between draft updates in milliseconds",
    )
    draft_min_delta_chars: int = Field(
        default=40,
        validation_alias=AliasChoices("TELEGRAM_DRAFT_MIN_DELTA_CHARS", "DRAFT_MIN_DELTA_CHARS"),
        description="Minimum meaningful character delta before sending draft updates",
    )
    draft_max_chars: int = Field(
        default=3500,
        validation_alias=AliasChoices("TELEGRAM_DRAFT_MAX_CHARS", "DRAFT_MAX_CHARS"),
        description="Maximum characters per draft update",
    )

    @field_validator("api_id", mode="before")
    @classmethod
    def _parse_api_id(cls, value: Any) -> int:
        if isinstance(value, int):
            api_id = value
        elif value is None or value == "":
            msg = "API ID is required"
            raise ValueError(msg)
        else:
            try:
                api_id = int(str(value))
            except ValueError as exc:  # pragma: no cover - defensive
                msg = "API ID must be a valid integer"
                raise ValueError(msg) from exc

        if api_id < 0:
            msg = "API ID must be non-negative"
            raise ValueError(msg)
        if api_id > 2**31 - 1:
            msg = "API ID too large"
            raise ValueError(msg)
        return api_id

    @field_validator("api_hash", mode="before")
    @classmethod
    def _validate_api_hash(cls, value: Any) -> str:
        api_hash = str(value or "")
        if not api_hash:
            return ""
        return _ensure_api_key(api_hash, name="API Hash")

    @field_validator("bot_token", mode="before")
    @classmethod
    def _validate_bot_token(cls, value: Any) -> str:
        token = str(value or "")
        if not token:
            return ""
        if ":" not in token:
            msg = "Bot token format appears invalid"
            raise ValueError(msg)
        parts = token.split(":")
        if len(parts) != 2:
            msg = "Bot token format appears invalid"
            raise ValueError(msg)
        if not parts[0].isdigit():
            msg = "Bot token ID part appears invalid"
            raise ValueError(msg)
        if len(parts[1]) < 30:
            msg = "Bot token secret part appears too short"
            raise ValueError(msg)
        return token

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _parse_allowed_users(cls, value: Any) -> tuple[int, ...]:
        return _parse_allowed_user_ids(value)

    @field_validator("draft_min_interval_ms", mode="before")
    @classmethod
    def _validate_draft_min_interval_ms(cls, value: Any) -> int:
        if value in (None, ""):
            return 700
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "Draft min interval must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 0 or parsed > 10000:
            msg = "Draft min interval must be between 0 and 10000ms"
            raise ValueError(msg)
        return parsed

    @field_validator("draft_min_delta_chars", mode="before")
    @classmethod
    def _validate_draft_min_delta_chars(cls, value: Any) -> int:
        if value in (None, ""):
            return 40
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "Draft min delta chars must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 1 or parsed > 1000:
            msg = "Draft min delta chars must be between 1 and 1000"
            raise ValueError(msg)
        return parsed

    @field_validator("draft_max_chars", mode="before")
    @classmethod
    def _validate_draft_max_chars(cls, value: Any) -> int:
        default = cls.model_fields["draft_max_chars"].default
        parsed = parse_positive_int(value, field_name="draft_max_chars", default=default)
        if parsed > 4096:
            msg = "Draft max chars must be 4096 or less"
            raise ValueError(msg)
        return parsed


class TelegramLimitsConfig(BaseModel):
    """Telegram message and URL limits configuration."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    max_message_chars: int = Field(
        default=3500,
        validation_alias="TELEGRAM_MAX_MESSAGE_CHARS",
        description="Maximum characters per Telegram message (Telegram limit ~4096, keep safety margin)",
    )
    max_url_length: int = Field(
        default=2048,
        validation_alias="TELEGRAM_MAX_URL_LENGTH",
        description="Maximum URL length (RFC 2616 limit)",
    )
    max_batch_urls: int = Field(
        default=200,
        validation_alias="TELEGRAM_MAX_BATCH_URLS",
        description="Maximum number of URLs in a batch operation",
    )
    min_message_interval_ms: int = Field(
        default=100,
        validation_alias="TELEGRAM_MIN_MESSAGE_INTERVAL_MS",
        description="Minimum interval between messages in milliseconds (rate limiting)",
    )

    @field_validator("max_message_chars", "max_url_length", "max_batch_urls", mode="before")
    @classmethod
    def _validate_positive_int(cls, value: Any, info: ValidationInfo) -> int:
        default = cls.model_fields[info.field_name].default
        return parse_positive_int(value, field_name=info.field_name, default=default)

    @field_validator("min_message_interval_ms", mode="before")
    @classmethod
    def _validate_message_interval(cls, value: Any) -> int:
        if value in (None, ""):
            return 100
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "Message interval must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 0:
            msg = "Message interval must be non-negative"
            raise ValueError(msg)
        if parsed > 10000:
            msg = "Message interval must be 10000ms or less"
            raise ValueError(msg)
        return parsed


class BatchProcessingConfig(BaseModel):
    """Configuration for batch URL processing."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    max_concurrent: int = Field(
        default=4,
        validation_alias="BATCH_MAX_CONCURRENT",
        description="Maximum concurrent URL processing tasks",
    )
    max_retries: int = Field(
        default=2,
        validation_alias="BATCH_MAX_RETRIES",
        description="Maximum retry attempts per URL",
    )
    domain_failfast_threshold: int = Field(
        default=2,
        validation_alias="BATCH_DOMAIN_FAILFAST_THRESHOLD",
        description="Number of failures before skipping remaining URLs from same domain",
    )
    initial_timeout_sec: float = Field(
        default=900.0,
        validation_alias="BATCH_INITIAL_TIMEOUT_SEC",
        description="Initial timeout for URL processing in seconds",
    )
    max_timeout_sec: float = Field(
        default=1800.0,
        validation_alias="BATCH_MAX_TIMEOUT_SEC",
        description="Maximum timeout cap for retries in seconds",
    )
    backoff_base: float = Field(
        default=3.0,
        validation_alias="BATCH_BACKOFF_BASE",
        description="Exponential backoff base between retries",
    )
    backoff_max: float = Field(
        default=60.0,
        validation_alias="BATCH_BACKOFF_MAX",
        description="Maximum backoff between retries in seconds",
    )
    state_ttl_sec: int = Field(
        default=120,
        validation_alias="BATCH_STATE_TTL_SEC",
        description="TTL for pending batch state in seconds",
    )

    @field_validator(
        "max_concurrent", "max_retries", "domain_failfast_threshold", "state_ttl_sec", mode="before"
    )
    @classmethod
    def _validate_positive_int_field(cls, value: Any, info: ValidationInfo) -> int:
        if value in (None, ""):
            default = cls.model_fields[info.field_name].default
            return int(default)
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = f"{info.field_name} must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 0:
            msg = f"{info.field_name} must be non-negative"
            raise ValueError(msg)
        return parsed

    @field_validator(
        "initial_timeout_sec", "max_timeout_sec", "backoff_base", "backoff_max", mode="before"
    )
    @classmethod
    def _validate_positive_float_field(cls, value: Any, info: ValidationInfo) -> float:
        if value in (None, ""):
            default = cls.model_fields[info.field_name].default
            return float(default)
        try:
            parsed = float(str(value))
        except ValueError as exc:
            msg = f"{info.field_name} must be a valid number"
            raise ValueError(msg) from exc
        if parsed <= 0:
            msg = f"{info.field_name} must be positive"
            raise ValueError(msg)
        return parsed
