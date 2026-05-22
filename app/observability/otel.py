"""OpenTelemetry SDK initialisation and helpers.

Guards all OTel imports so the module is importable even when the
[otel] extra is not installed — every public function degrades to a no-op.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import contextlib

    from app.config.settings import AppConfig

_initialized = False
_otel_available = False
_HTTP_CAPTURE_SANITIZE_ENV = "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS"
_SENSITIVE_HTTP_HEADER_SANITIZERS = (
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-github-token",
    ".*token.*",
    ".*secret.*",
)

try:
    from opentelemetry import trace as _trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased

    _otel_available = True
except ImportError:
    pass


def _is_enabled(cfg: AppConfig | None) -> bool:
    if cfg is not None:
        return cfg.otel.enabled
    return os.getenv("OTEL_ENABLED", "").lower() in ("1", "true")


def _ensure_http_header_sanitizers() -> None:
    """Ensure OTel HTTP header capture redacts auth and token-like fields."""
    configured = [
        item.strip()
        for item in os.getenv(_HTTP_CAPTURE_SANITIZE_ENV, "").split(",")
        if item.strip()
    ]
    lower_configured = {item.lower() for item in configured}
    merged = [
        *configured,
        *[
            sanitizer
            for sanitizer in _SENSITIVE_HTTP_HEADER_SANITIZERS
            if sanitizer.lower() not in lower_configured
        ],
    ]
    os.environ[_HTTP_CAPTURE_SANITIZE_ENV] = ",".join(merged)


def init_tracing(cfg: AppConfig | None = None) -> None:
    """Initialise the OTel SDK.  No-op when [otel] extra is absent or OTEL_ENABLED=false.

    Call once per process, before any httpx/redis client is constructed.
    Subsequent calls are silently ignored (idempotent).
    """
    global _initialized
    if _initialized:
        return
    if not _otel_available or not _is_enabled(cfg):
        return
    _ensure_http_header_sanitizers()

    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    endpoint = (
        cfg.otel.endpoint
        if cfg is not None
        else os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4317")
    )
    process_role = os.getenv("RATATOSKR_PROCESS_ROLE", "unknown")
    service_version = os.getenv("RATATOSKR_VERSION", "0.1.0")
    deploy_env = os.getenv("RATATOSKR_ENV", "production")

    resource = Resource.create(
        {
            "service.name": "ratatoskr",
            "service.version": service_version,
            "deployment.environment": deploy_env,
            "service.instance.id": process_role,
        }
    )

    provider = TracerProvider(resource=resource, sampler=ParentBased(ALWAYS_ON))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, insecure=True),
            max_queue_size=2048,
            max_export_batch_size=512,
            schedule_delay_millis=5000,
        )
    )
    _trace.set_tracer_provider(provider)

    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    LoggingInstrumentor().instrument(set_logging_format=False)

    _initialized = True


def shutdown_tracing() -> None:
    """Flush and shut down the tracer provider (call in process shutdown)."""
    if not _otel_available or not _initialized:
        return
    provider = _trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()


def get_tracer(name: str) -> Any:
    """Return an OTel Tracer (or a no-op tracer when OTel is not installed)."""
    if not _otel_available:
        return _NoOpTracer()
    return _trace.get_tracer(name)


def set_correlation_id_attr(cid: str | None) -> None:
    """Attach correlation_id to the current active span as a span attribute."""
    if not _otel_available or not cid:
        return
    span = _trace.get_current_span()
    if span.is_recording():
        span.set_attribute("ratatoskr.correlation_id", cid)


class _NoOpTracer:
    """Minimal no-op tracer returned when OTel SDK is not installed."""

    def start_as_current_span(
        self, name: str, **kwargs: Any
    ) -> contextlib.AbstractContextManager[_NoOpSpan]:
        import contextlib

        return contextlib.nullcontext(_NoOpSpan())

    def start_span(self, name: str, **kwargs: Any) -> Any:
        return _NoOpSpan()


class _NoOpSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def record_exception(self, exc: Exception) -> None:
        pass

    def set_status(self, status: Any) -> None:
        pass

    def is_recording(self) -> bool:
        return False

    def __enter__(self) -> _NoOpSpan:
        return self

    def __exit__(self, *args: Any) -> None:
        pass
