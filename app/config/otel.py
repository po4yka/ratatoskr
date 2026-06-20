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
    # sample_ratio is retained for documentation but has no effect: the
    # sampler is hard-wired to ParentBased(ALWAYS_ON) (100 % sampling).
    # Removing this field would be a breaking env-var change, so it is kept.
    sample_ratio: float = Field(default=1.0, validation_alias="OTEL_SAMPLE_RATIO")

    # Exporter backend selector.
    # Allowed values: "otlp" (default) | "console" | "file"
    # Changing this value requires no instrumentation changes; it only
    # affects which SpanExporter is wired inside init_tracing().
    traces_exporter: str = Field(
        default="otlp",
        validation_alias="OTEL_TRACES_EXPORTER",
    )

    # Filesystem path for the file exporter (used only when traces_exporter="file").
    # The directory is created on first write if it does not exist.
    file_exporter_path: str = Field(
        default="/data/traces/spans.jsonl",
        validation_alias="OTEL_FILE_EXPORTER_PATH",
    )

    # Controls whether the OTLP gRPC exporter uses an insecure (plaintext)
    # channel.  Defaults to True to preserve backward compatibility with the
    # default local Tempo deployment (http://tempo:4317).  Set to False when
    # exporting to a TLS-terminating endpoint (HTTPS / port 443).
    otel_insecure: bool = Field(
        default=True,
        validation_alias="OTEL_EXPORTER_OTLP_INSECURE",
    )

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
