"""OpenTelemetry SDK initialisation and helpers.

Guards all OTel imports so the module is importable even when the
[otel] extra is not installed — every public function degrades to a no-op.

Exporter selection
------------------
Set OTEL_TRACES_EXPORTER to one of the following values (default: "otlp"):

  otlp     -- gRPC OTLP export to OTEL_EXPORTER_OTLP_ENDPOINT (default http://tempo:4317)
  console  -- Writes human-readable spans to stdout; useful for local debugging
  file     -- Appends JSON span lines to OTEL_FILE_EXPORTER_PATH (default /data/traces/spans.jsonl)

Swapping the exporter requires only an environment variable change; no
instrumentation code needs to change.
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.config.settings import AppConfig

_initialized = False
_otel_available = False
_instrumented_fastapi_app_ids: set[int] = set()
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


def _build_exporter(cfg: AppConfig | None) -> Any:
    """Construct and return the configured span exporter.

    Reads OTEL_TRACES_EXPORTER (or cfg.otel.traces_exporter) and returns one of:
      - OTLPSpanExporter  (default, value "otlp")
      - ConsoleSpanExporter (value "console")
      - _FileSpanExporter   (value "file")

    The returned exporter is always wrapped in BatchSpanProcessor by the caller.
    Export failures are isolated by BatchSpanProcessor and never reach the
    request path.
    """
    exporter_name = os.getenv("OTEL_TRACES_EXPORTER", "otlp").lower()
    if cfg is not None and hasattr(cfg.otel, "traces_exporter"):
        exporter_name = (cfg.otel.traces_exporter or exporter_name).lower()

    if exporter_name == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter()

    if exporter_name == "file":
        return _FileSpanExporter(
            path=os.getenv("OTEL_FILE_EXPORTER_PATH", "/data/traces/spans.jsonl")
        )

    # Default: OTLP gRPC
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    endpoint = (
        cfg.otel.endpoint
        if cfg is not None
        else os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4317")
    )
    return OTLPSpanExporter(endpoint=endpoint, insecure=True)


def init_tracing(cfg: AppConfig | None = None, *, fastapi_app: Any | None = None) -> None:
    """Initialise the OTel SDK.  No-op when [otel] extra is absent or OTEL_ENABLED=false.

    Call once per process, before any httpx/redis client is constructed.
    Subsequent calls are silently ignored (idempotent).

    Sampler: ParentBased(ALWAYS_ON) — 100% sampling.  The sampler is not
    configurable; if probabilistic sampling is ever needed, swap this line
    rather than adding a separate code path.

    Exporter: selected via OTEL_TRACES_EXPORTER; see module docstring.
    """
    global _initialized
    if _initialized:
        if fastapi_app is not None:
            instrument_fastapi_app(fastapi_app, cfg=cfg)
        return
    if not _otel_available or not _is_enabled(cfg):
        return
    _ensure_http_header_sanitizers()

    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

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

    # 100% sampling — ParentBased(ALWAYS_ON) means: if a parent span exists,
    # honour its sampling decision; if this is a root span, always sample.
    provider = TracerProvider(resource=resource, sampler=ParentBased(ALWAYS_ON))
    provider.add_span_processor(
        BatchSpanProcessor(
            _build_exporter(cfg),
            max_queue_size=2048,
            max_export_batch_size=512,
            schedule_delay_millis=5000,
        )
    )
    _trace.set_tracer_provider(provider)

    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    LoggingInstrumentor().instrument(set_logging_format=False)
    if fastapi_app is not None:
        instrument_fastapi_app(fastapi_app, cfg=cfg)

    _initialized = True


def instrument_fastapi_app(app: Any, *, cfg: AppConfig | None = None) -> None:
    """Auto-instrument a FastAPI app when tracing is enabled.

    Kept in this module so HTTP server instrumentation is configured beside the
    other OpenTelemetry instrumentors. The helper is idempotent per app object.
    """
    if not _otel_available or not _is_enabled(cfg):
        return
    app_id = id(app)
    if app_id in _instrumented_fastapi_app_ids:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except ImportError:
        return
    _instrumented_fastapi_app_ids.add(app_id)


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
    from app.observability.attributes import REQUEST_CORRELATION_ID

    span = _trace.get_current_span()
    if span.is_recording():
        span.set_attribute(REQUEST_CORRELATION_ID, cid)


def set_user_id_attr(user_id: int | str | None) -> None:
    """Attach user_id to the current active span as a span attribute."""
    if not _otel_available or user_id is None:
        return
    from app.observability.attributes import REQUEST_USER_ID

    span = _trace.get_current_span()
    if span.is_recording():
        span.set_attribute(REQUEST_USER_ID, user_id)


# ---------------------------------------------------------------------------
# Telethon manual span-wrap helper
# ---------------------------------------------------------------------------
# Telethon has no OTel auto-instrumentation.  Wrap Telethon coroutine calls
# that matter for latency visibility using this helper.
#
# Usage (inside an async function)::
#
#     from app.observability.otel import telethon_span
#
#     async with telethon_span("telethon.send_message", correlation_id=cid) as span:
#         span.set_attribute("ratatoskr.telegram.chat_id", str(chat_id))
#         await client.send_message(chat, text)
#
# The context manager is a no-op when OTel is not installed or OTEL_ENABLED
# is false, so it is safe to use unconditionally.


@contextlib.asynccontextmanager
async def telethon_span(
    span_name: str,
    *,
    correlation_id: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> Any:
    """Async context manager that opens a named OTel span for a Telethon call.

    Args:
        span_name: Span name, e.g. "telethon.send_message".
        correlation_id: Optional correlation ID to attach as
            ``ratatoskr.correlation_id``.
        attributes: Optional mapping of additional span attributes to set
            immediately on span open.

    Yields:
        The live span (OTel Span or _NoOpSpan when OTel is absent).

    Example::

        async with telethon_span("telethon.send_message", correlation_id=cid) as span:
            span.set_attribute("ratatoskr.telegram.chat_id", str(chat_id))
            await bot.send_message(chat_id, text)
    """
    tracer = get_tracer("ratatoskr.telethon")
    async with tracer.start_as_current_span(span_name) as span:
        if correlation_id:
            set_correlation_id_attr(correlation_id)
        if attributes:
            for key, value in attributes.items():
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            if _otel_available:
                from opentelemetry.trace import Status, StatusCode

                span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


# ---------------------------------------------------------------------------
# File span exporter (OTEL_TRACES_EXPORTER=file)
# ---------------------------------------------------------------------------


class _FileSpanExporter:
    """Minimal span exporter that appends JSON lines to a file.

    Each exported span is written as a single JSON object followed by a
    newline.  The file is opened in append mode for each export batch so
    process restarts do not truncate history.

    This exporter is intended for low-volume single-tenant deployments where
    Tempo is unavailable (e.g. RAM-constrained Pi).  Traces can be queried
    offline with DuckDB::

        SELECT * FROM read_json_auto('/data/traces/spans.jsonl')
        WHERE attributes['ratatoskr.correlation_id'] = '<cid>';
    """

    def __init__(self, path: str) -> None:
        self._path = path

    def export(self, spans: Any) -> Any:  # SpanExportResult
        try:
            import json
            import pathlib

            pathlib.Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                for span in spans:
                    record: dict[str, Any] = {
                        "name": span.name,
                        "trace_id": format(span.context.trace_id, "032x"),
                        "span_id": format(span.context.span_id, "016x"),
                        "start_time": span.start_time,
                        "end_time": span.end_time,
                        "status": span.status.status_code.name,
                        "attributes": dict(span.attributes or {}),
                    }
                    fh.write(json.dumps(record) + "\n")
        except Exception:
            pass  # Export failures must never surface into the request path
        if _otel_available:
            from opentelemetry.sdk.trace.export import SpanExportResult

            return SpanExportResult.SUCCESS
        return None

    def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# No-op fallback classes (OTel SDK absent or OTEL_ENABLED=false)
# ---------------------------------------------------------------------------


class _NoOpTracer:
    """Minimal no-op tracer returned when OTel SDK is not installed."""

    def start_as_current_span(
        self, name: str, **kwargs: Any
    ) -> contextlib.AbstractContextManager[_NoOpSpan]:
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
