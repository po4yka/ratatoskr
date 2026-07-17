from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DeploymentConfig(BaseModel):
    """Deployment environment and production-safety configuration."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    env: Literal["development", "staging", "production"] = Field(
        default="development",
        validation_alias="APP_ENV",
        description=(
            "Deployment environment. Set to 'production' to enable strict safety checks, "
            "including mandatory Redis-backed rate limiting."
        ),
    )
    api_public_exposure: bool = Field(
        default=False,
        validation_alias="API_PUBLIC_EXPOSURE",
        description=(
            "Set to true when the API is reachable from the public internet. "
            "Triggers production-level safety checks regardless of APP_ENV."
        ),
    )
    rate_limit_redis_override: bool = Field(
        default=False,
        validation_alias="RATE_LIMIT_REDIS_OVERRIDE",
        description=(
            "Emergency override: allow in-memory rate limiting in production. "
            "Must be explicitly set to acknowledge that multi-worker deployments "
            "will have per-process rate limit state (limits are not shared)."
        ),
    )
    status_bot_metrics_url: str | None = Field(
        default=None,
        validation_alias="STATUS_BOT_METRICS_URL",
        description="Internal HTTP metrics endpoint used to probe the Telegram bot process.",
    )
    status_worker_metrics_url: str | None = Field(
        default=None,
        validation_alias="STATUS_WORKER_METRICS_URL",
        description="Internal HTTP metrics endpoint used to probe the Taskiq worker process.",
    )
    status_scheduler_metrics_url: str | None = Field(
        default=None,
        validation_alias="STATUS_SCHEDULER_METRICS_URL",
        description="Internal HTTP metrics endpoint used to probe the scheduler process.",
    )
    status_node_metrics_url: str | None = Field(
        default=None,
        validation_alias="STATUS_NODE_METRICS_URL",
        description="Internal node-exporter endpoint used to probe PostgreSQL backup freshness.",
    )
    status_qdrant_ready_url: str | None = Field(
        default=None,
        validation_alias="STATUS_QDRANT_READY_URL",
        description="Internal Qdrant readiness endpoint used by the public status evaluator.",
    )
    status_probe_timeout_seconds: float = Field(
        default=1.5,
        validation_alias="STATUS_PROBE_TIMEOUT_SECONDS",
        gt=0,
        le=5,
    )
    status_total_timeout_seconds: float = Field(
        default=4.5,
        validation_alias="STATUS_TOTAL_TIMEOUT_SECONDS",
        gt=0,
        le=5,
    )
    status_cache_ttl_seconds: int = Field(
        default=20,
        validation_alias="STATUS_CACHE_TTL_SECONDS",
        ge=15,
        le=30,
    )
    status_refresh_after_seconds: int = Field(
        default=30,
        validation_alias="STATUS_REFRESH_AFTER_SECONDS",
        ge=15,
        le=300,
    )

    @field_validator("env", mode="before")
    @classmethod
    def _validate_env(cls, value: Any) -> str:
        if value in (None, ""):
            return "development"
        v = str(value).strip().lower()
        allowed = ("development", "staging", "production")
        if v not in allowed:
            msg = f"APP_ENV must be one of: {', '.join(allowed)}"
            raise ValueError(msg)
        return v

    @field_validator(
        "status_bot_metrics_url",
        "status_worker_metrics_url",
        "status_scheduler_metrics_url",
        "status_node_metrics_url",
        "status_qdrant_ready_url",
        mode="before",
    )
    @classmethod
    def _validate_status_metrics_url(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        url = str(value).strip()
        if len(url) > 512:
            raise ValueError("status metrics URL is too long")
        try:
            parsed = urlsplit(url)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("status metrics URL is invalid") from exc
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "status metrics URL must be an internal http URL without credentials, query, or fragment"
            )
        return url

    @model_validator(mode="after")
    def _validate_status_timeouts(self) -> DeploymentConfig:
        if self.status_probe_timeout_seconds > self.status_total_timeout_seconds:
            raise ValueError(
                "STATUS_PROBE_TIMEOUT_SECONDS must not exceed STATUS_TOTAL_TIMEOUT_SECONDS"
            )
        return self

    @property
    def is_production_mode(self) -> bool:
        """True when running in production or with public API exposure."""
        return self.env == "production" or self.api_public_exposure
