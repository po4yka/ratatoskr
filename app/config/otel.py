"""OpenTelemetry and Sentry observability configuration."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from ._secret_marker import SECRET_MARKER


class OtelConfig(BaseSettings):
    model_config = {"populate_by_name": True, "extra": "ignore"}

    enabled: bool = Field(default=False, validation_alias="OTEL_ENABLED")
    endpoint: str = Field(
        default="http://tempo:4317",
        validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT",
    )
    db_session_spans_enabled: bool = Field(
        default=False, validation_alias="OTEL_DB_SESSION_SPANS_ENABLED"
    )
    sample_ratio: float = Field(default=1.0, validation_alias="OTEL_SAMPLE_RATIO")

    @classmethod
    def from_env(cls) -> OtelConfig:
        return cls()


class SentryConfig(BaseSettings):
    """Sentry error-monitoring configuration.

    All fields default to safe no-op values so Sentry is disabled unless
    SENTRY_DSN is explicitly set in the environment.
    """

    model_config = {"populate_by_name": True, "extra": "ignore"}

    sentry_dsn: str | None = Field(
        default=None, validation_alias="SENTRY_DSN", json_schema_extra=SECRET_MARKER
    )
    traces_sample_rate: float = Field(default=0.1, validation_alias="SENTRY_TRACES_SAMPLE_RATE")

    @classmethod
    def from_env(cls) -> SentryConfig:
        return cls()
