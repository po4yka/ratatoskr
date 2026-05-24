from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from ._secret_marker import SECRET_MARKER
from ._validators import _ensure_api_key


class FirecrawlConfig(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    api_key: str = Field(
        default="",
        validation_alias="FIRECRAWL_API_KEY",
        description="Firecrawl API key (optional when using only Scrapling + self-hosted)",
        json_schema_extra=SECRET_MARKER,
    )
    timeout_sec: int = Field(
        default=90,
        validation_alias="FIRECRAWL_TIMEOUT_SEC",
        description="Request timeout in seconds (default 90, increased from 60 for better reliability)",
    )
    wait_for_ms: int = Field(
        default=3000,
        validation_alias="FIRECRAWL_WAIT_FOR_MS",
        description="Wait for JS content to load in milliseconds (default 3000)",
    )
    max_connections: int = Field(default=10, validation_alias="FIRECRAWL_MAX_CONNECTIONS")
    max_keepalive_connections: int = Field(
        default=5, validation_alias="FIRECRAWL_MAX_KEEPALIVE_CONNECTIONS"
    )
    keepalive_expiry: float = Field(default=30.0, validation_alias="FIRECRAWL_KEEPALIVE_EXPIRY")
    retry_max_attempts: int = Field(default=3, validation_alias="FIRECRAWL_RETRY_MAX_ATTEMPTS")
    retry_initial_delay: float = Field(
        default=1.0, validation_alias="FIRECRAWL_RETRY_INITIAL_DELAY"
    )
    retry_max_delay: float = Field(default=10.0, validation_alias="FIRECRAWL_RETRY_MAX_DELAY")
    retry_backoff_factor: float = Field(
        default=2.0, validation_alias="FIRECRAWL_RETRY_BACKOFF_FACTOR"
    )
    credit_warning_threshold: int = Field(
        default=1000, validation_alias="FIRECRAWL_CREDIT_WARNING_THRESHOLD"
    )
    credit_critical_threshold: int = Field(
        default=100, validation_alias="FIRECRAWL_CREDIT_CRITICAL_THRESHOLD"
    )
    max_response_size_mb: int = Field(default=50, validation_alias="FIRECRAWL_MAX_RESPONSE_SIZE_MB")
    max_age_seconds: int = Field(default=172_800, validation_alias="FIRECRAWL_MAX_AGE_SECONDS")
    remove_base64_images: bool = Field(
        default=True, validation_alias="FIRECRAWL_REMOVE_BASE64_IMAGES"
    )
    block_ads: bool = Field(default=True, validation_alias="FIRECRAWL_BLOCK_ADS")
    skip_tls_verification: bool = Field(
        default=True, validation_alias="FIRECRAWL_SKIP_TLS_VERIFICATION"
    )
    include_markdown_format: bool = Field(
        default=True, validation_alias="FIRECRAWL_INCLUDE_MARKDOWN"
    )
    include_html_format: bool = Field(default=True, validation_alias="FIRECRAWL_INCLUDE_HTML")
    include_links_format: bool = Field(default=False, validation_alias="FIRECRAWL_INCLUDE_LINKS")
    include_summary_format: bool = Field(
        default=False, validation_alias="FIRECRAWL_INCLUDE_SUMMARY"
    )
    include_images_format: bool = Field(default=True, validation_alias="FIRECRAWL_INCLUDE_IMAGES")
    enable_screenshot_format: bool = Field(
        default=False, validation_alias="FIRECRAWL_ENABLE_SCREENSHOT"
    )
    screenshot_full_page: bool = Field(
        default=True, validation_alias="FIRECRAWL_SCREENSHOT_FULL_PAGE"
    )
    screenshot_quality: int = Field(default=80, validation_alias="FIRECRAWL_SCREENSHOT_QUALITY")
    screenshot_viewport_width: int | None = Field(
        default=None, validation_alias="FIRECRAWL_SCREENSHOT_VIEWPORT_WIDTH"
    )
    screenshot_viewport_height: int | None = Field(
        default=None, validation_alias="FIRECRAWL_SCREENSHOT_VIEWPORT_HEIGHT"
    )
    json_prompt: str | None = Field(default=None, validation_alias="FIRECRAWL_JSON_PROMPT")
    json_schema: dict[str, Any] | None = Field(
        default=None, validation_alias="FIRECRAWL_JSON_SCHEMA"
    )

    @field_validator("api_key", mode="before")
    @classmethod
    def _validate_api_key(cls, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        return _ensure_api_key(raw, name="Firecrawl")

    @field_validator(
        "timeout_sec",
        "wait_for_ms",
        "max_connections",
        "max_keepalive_connections",
        "retry_max_attempts",
        "credit_warning_threshold",
        "credit_critical_threshold",
        "max_response_size_mb",
        "max_age_seconds",
        mode="before",
    )
    @classmethod
    def _parse_int_bounds(cls, value: Any, info: ValidationInfo) -> int:
        if value in (None, ""):
            default = cls.model_fields[info.field_name].default
            if default is None:
                msg = f"{info.field_name.replace('_', ' ')} is required"
                raise ValueError(msg)
            return int(default)
        try:
            parsed = int(str(value))
        except ValueError as exc:  # pragma: no cover - defensive
            msg = f"{info.field_name.replace('_', ' ')} must be a valid integer"
            raise ValueError(msg) from exc

        limits: dict[str, tuple[int, int]] = {
            "timeout_sec": (10, 300),  # 10 seconds to 5 minutes
            "wait_for_ms": (0, 30000),  # 0 to 30 seconds
            "max_connections": (1, 100),
            "max_keepalive_connections": (1, 50),
            "retry_max_attempts": (0, 10),
            "credit_warning_threshold": (1, 10000),
            "credit_critical_threshold": (1, 1000),
            "max_response_size_mb": (1, 1024),
            "max_age_seconds": (60, 2_592_000),  # 1 minute to 30 days
        }
        min_val, max_val = limits[info.field_name]
        if parsed < min_val or parsed > max_val:
            msg = f"{info.field_name.replace('_', ' ').capitalize()} must be between {min_val} and {max_val}"
            raise ValueError(msg)
        return parsed

    @field_validator(
        "keepalive_expiry",
        "retry_initial_delay",
        "retry_max_delay",
        "retry_backoff_factor",
        mode="before",
    )
    @classmethod
    def _parse_float_bounds(cls, value: Any, info: ValidationInfo) -> float:
        if value in (None, ""):
            default = cls.model_fields[info.field_name].default
            if default is None:
                msg = f"{info.field_name.replace('_', ' ')} is required"
                raise ValueError(msg)
            return float(default)
        try:
            parsed = float(str(value))
        except ValueError as exc:  # pragma: no cover - defensive
            msg = f"{info.field_name.replace('_', ' ')} must be a valid number"
            raise ValueError(msg) from exc

        limits: dict[str, tuple[float, float]] = {
            "keepalive_expiry": (1.0, 300.0),
            "retry_initial_delay": (0.1, 60.0),
            "retry_max_delay": (1.0, 300.0),
            "retry_backoff_factor": (1.0, 10.0),
        }
        min_val, max_val = limits[info.field_name]
        if parsed < min_val or parsed > max_val:
            msg = f"{info.field_name.replace('_', ' ').capitalize()} must be between {min_val} and {max_val}"
            raise ValueError(msg)
        return parsed

    @field_validator("screenshot_quality", mode="before")
    @classmethod
    def _validate_screenshot_quality(cls, value: Any) -> int:
        if value in (None, ""):
            return 80
        try:
            parsed = int(str(value))
        except ValueError as exc:  # pragma: no cover - defensive
            msg = "Screenshot quality must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 1 or parsed > 100:
            msg = "Screenshot quality must be between 1 and 100"
            raise ValueError(msg)
        return parsed

    @field_validator("json_prompt", mode="before")
    @classmethod
    def _strip_json_prompt(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        text = str(value).strip()
        return text or None
