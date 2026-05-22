from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


class BackgroundProcessorConfig(BaseModel):
    """Background processor settings."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    redis_lock_enabled: bool = Field(default=True, validation_alias="BACKGROUND_REDIS_LOCK_ENABLED")
    redis_lock_required: bool = Field(
        default=False,
        validation_alias="BACKGROUND_REDIS_LOCK_REQUIRED",
        description="If true, fail processing when Redis is unavailable.",
    )
    lock_ttl_ms: int = Field(default=300_000, validation_alias="BACKGROUND_LOCK_TTL_MS")
    lock_skip_on_held: bool = Field(default=True, validation_alias="BACKGROUND_LOCK_SKIP_ON_HELD")
    retry_attempts: int = Field(default=3, validation_alias="BACKGROUND_RETRY_ATTEMPTS")
    retry_base_delay_ms: int = Field(default=500, validation_alias="BACKGROUND_RETRY_BASE_DELAY_MS")
    retry_max_delay_ms: int = Field(default=5_000, validation_alias="BACKGROUND_RETRY_MAX_DELAY_MS")
    retry_jitter_ratio: float = Field(default=0.2, validation_alias="BACKGROUND_RETRY_JITTER_RATIO")
    durable_worker_enabled: bool = Field(
        default=True, validation_alias="BACKGROUND_DURABLE_WORKER_ENABLED"
    )
    durable_lease_ttl_seconds: int = Field(
        default=300, validation_alias="BACKGROUND_DURABLE_LEASE_TTL_SECONDS"
    )
    durable_retry_delay_seconds: int = Field(
        default=30, validation_alias="BACKGROUND_DURABLE_RETRY_DELAY_SECONDS"
    )
    durable_poll_interval_ms: int = Field(
        default=1_000, validation_alias="BACKGROUND_DURABLE_POLL_INTERVAL_MS"
    )
    stuck_processing_seconds: int = Field(
        default=900, validation_alias="BACKGROUND_STUCK_PROCESSING_SECONDS"
    )

    @field_validator(
        "lock_ttl_ms",
        "retry_attempts",
        "retry_base_delay_ms",
        "retry_max_delay_ms",
        "durable_lease_ttl_seconds",
        "durable_retry_delay_seconds",
        "durable_poll_interval_ms",
        "stuck_processing_seconds",
    )
    @classmethod
    def _validate_positive_int(cls, value: Any, info: ValidationInfo) -> int:
        default = cls.model_fields[info.field_name].default
        try:
            parsed = int(str(value if value not in (None, "") else default))
        except ValueError as exc:  # pragma: no cover - defensive
            msg = f"{info.field_name.replace('_', ' ')} must be a valid integer"
            raise ValueError(msg) from exc
        limits: dict[str, tuple[int, int]] = {
            "lock_ttl_ms": (1_000, 3_600_000),
            "retry_attempts": (1, 10),
            "retry_base_delay_ms": (50, 60_000),
            "retry_max_delay_ms": (100, 300_000),
            "durable_lease_ttl_seconds": (10, 86_400),
            "durable_retry_delay_seconds": (1, 86_400),
            "durable_poll_interval_ms": (100, 60_000),
            "stuck_processing_seconds": (60, 604_800),
        }
        min_val, max_val = limits.get(info.field_name, (1, 3_600_000))
        if parsed < min_val or parsed > max_val:
            msg = (
                f"{info.field_name.replace('_', ' ').capitalize()} must be between "
                f"{min_val} and {max_val}"
            )
            raise ValueError(msg)
        return parsed

    @field_validator("retry_jitter_ratio", mode="before")
    @classmethod
    def _validate_jitter(cls, value: Any) -> float:
        default = cls.model_fields["retry_jitter_ratio"].default
        try:
            parsed = float(str(value if value not in (None, "") else default))
        except ValueError as exc:  # pragma: no cover - defensive
            msg = "Background retry jitter ratio must be a valid number"
            raise ValueError(msg) from exc
        if parsed < 0 or parsed > 1:
            msg = "Background retry jitter ratio must be between 0 and 1"
            raise ValueError(msg)
        return parsed

    @model_validator(mode="after")
    def _validate_retry_delay_order(self) -> BackgroundProcessorConfig:
        """Ensure retry_base_delay_ms <= retry_max_delay_ms."""
        if self.retry_base_delay_ms > self.retry_max_delay_ms:
            msg = (
                f"retry_base_delay_ms ({self.retry_base_delay_ms}) must be <= "
                f"retry_max_delay_ms ({self.retry_max_delay_ms})"
            )
            raise ValueError(msg)
        return self
