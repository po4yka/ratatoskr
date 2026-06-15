"""Structured logging utilities.

Convention: use get_logger() / log_exception() for all persistent observability output.
print() is acceptable only in CLI tools (app/cli/), interactive gRPC clients, and
docstring examples. All bot and API code must use structured logging to ensure
correlation IDs and log levels are preserved.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import orjson
from loguru import logger as loguru_logger

_REDACTED = "[REDACTED]"
_CONTENT_REDACTED = "[REDACTED_CONTENT]"
_SENSITIVE_KEY_RE = re.compile(
    r"(authorization|cookie|set[-_]?cookie|access[-_]?token|refresh[-_]?token|api[-_]?key|"
    r"telegram[-_]?token|github[-_]?token|bot[-_]?token|x[-_]?api[-_]?key|"
    r"personal[-_]?access[-_]?token|client[-_]?secret|device[-_]?code|secret)",
    re.IGNORECASE,
)
_TOKEN_KEY_RE = re.compile(r"(^|[-_])token($|[-_])", re.IGNORECASE)
_CONTENT_KEY_RE = re.compile(
    r"(^content$|raw[-_]?source|source[-_]?content|prompt|messages|request[-_]?messages|"
    r"response[-_]?text|raw[-_]?body|html|markdown|text[-_]?preview|content[-_]?preview|"
    r"prompt[-_]?preview|payload[-_]?preview)",
    re.IGNORECASE,
)
_DEBUG_PREVIEW_KEY_RE = re.compile(r"^debug[-_].*preview$", re.IGNORECASE)
_URL_KEY_RE = re.compile(
    r"(^url$|source[-_]?url|input[-_]?url|normalized[-_]?url|canonical[-_]?url|resolved[-_]?url)",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(
    r"\b(authorization|cookie|set-cookie|x-api-key)\s*[:=]\s*([^\s,;\n]+)",
    re.IGNORECASE,
)
_TOKEN_ASSIGNMENT_RE = re.compile(
    r"\b(access_token|refresh_token|authorization_code|oauth_code|oauth_state|api_key|"
    r"telegram_token|github_token|bot_token|personal_access_token|pat|client_secret|"
    r"device_code|cookie|cookies|code|state|token|secret)"
    r"\s*[:=]\s*([A-Za-z0-9._~:/+\-=]{8,})",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\b(Bearer|Token|Bot)\s+[A-Za-z0-9._~:/+\-=]{8,}", re.IGNORECASE)
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"\b\d{5,12}:[A-Za-z0-9_-]{20,}\b")
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_API_KEY_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|sk-or-[A-Za-z0-9_-]{12,}|fc-[A-Za-z0-9_-]{12,})\b"
)
_URL_WITH_SECRET_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_URL_SECRET_QUERY_KEYS = {
    "access_token",
    "auth",
    "authorization_code",
    "code",
    "key",
    "oauth_code",
    "oauth_state",
    "password",
    "refresh_token",
    "secret",
    "sig",
    "signature",
    "state",
    "token",
}
_OPERATIONAL_KEY_EXACT = {
    "completion_tokens",
    "prompt_tokens",
    "tokens_completion",
    "tokens_prompt",
    "tokens_total",
    "total_tokens",
}
_SENSITIVE_KEY_EXACT = {
    "authorization_code",
    "code",
    "oauth_code",
    "oauth_state",
    "pat",
    "state",
}


def _privacy_redact_urls_default() -> bool:
    raw = os.getenv("LOG_PRIVACY_REDACT_URLS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _json_sink(message: Any) -> None:
    """Custom loguru sink that writes structured JSON to stdout via orjson."""
    record = message.record
    log_entry: dict[str, Any] = {
        "timestamp": record["time"].strftime("%Y-%m-%dT%H:%M:%S.%f%z"),
        "level": record["level"].name,
        "logger": record["name"],
        "message": redact_for_logging(record["message"]),
        "module": record["module"],
        "function": record["function"],
        "line": record["line"],
        "process": record["process"].id,
        "thread": record["thread"].id,
    }
    # Merge extra fields (correlation_id, etc.)
    for k, v in record["extra"].items():
        if k not in log_entry:
            log_entry[k] = redact_for_logging(v, key=k)
    # Rename OTel-injected fields to snake_case for Grafana Tempo derived-fields
    if (otel_trace_id := log_entry.get("otelTraceID")) and otel_trace_id != "0":
        log_entry["trace_id"] = otel_trace_id
        log_entry["span_id"] = log_entry.pop("otelSpanID", "")
        log_entry.pop("otelTraceID", None)
        log_entry.pop("otelTraceSampled", None)
        log_entry.pop("otelServiceName", None)
    # Include exception info when present
    if record["exception"] is not None:
        log_entry["exception"] = redact_for_logging(str(message).rstrip("\n"))
    try:
        data = orjson.dumps(log_entry)
    except (TypeError, ValueError):
        # Fallback: stringify non-serializable values and retry
        for k, v in log_entry.items():
            if not isinstance(v, (str, int, float, bool, type(None))):
                log_entry[k] = repr(v)
        try:
            data = orjson.dumps(log_entry)
        except Exception:
            # Last resort: emit minimal plain-text line
            data = f'{{"level":"{record["level"].name}","message":"{record["message"]}"}}'.encode()
    sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()


def setup_json_logging(
    level: str = "INFO",
    include_location: bool = True,
    include_process_info: bool = True,
    log_file: str | None = None,
    max_file_size: str = "100 MB",
    retention: str = "30 days",
) -> None:
    """Configure enhanced JSON logging via loguru with optional file output.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        include_location: Include file/line information in logs
        include_process_info: Include process/thread information
        log_file: Optional log file path for persistent logging
        max_file_size: Maximum size per log file (loguru format)
        retention: Log retention period (loguru format)

    """
    lvl = getattr(logging, level.upper(), logging.INFO)

    # Remove existing handlers to avoid duplicate logs
    try:
        loguru_logger.remove()
    except Exception as exc:  # pragma: no cover
        get_logger(__name__).debug("loguru_handler_remove_failed: %s", exc)

    # Add console sink -- custom function builds JSON via orjson
    loguru_logger.add(
        _json_sink,
        level=level.upper(),
        enqueue=True,  # Thread-safe logging
        backtrace=True,
        diagnose=True,
    )

    # Add file sink if specified -- serialize=True alone (without a custom format
    # string) produces loguru's built-in JSON schema, which is safe and correct.
    if log_file:
        loguru_logger.add(
            log_file,
            level=level.upper(),
            serialize=True,
            rotation=max_file_size,
            retention=retention,
            compression="gz",
            enqueue=True,
            backtrace=True,
            diagnose=True,
        )

    # Enhanced bridge for stdlib logging
    class EnhancedInterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            level_to_use: int | str
            try:
                level_to_use = loguru_logger.level(record.levelname).name
            except Exception:
                level_to_use = record.levelno

            # Extract extra fields
            extra = {}
            standard_fields = {
                "args",
                "msg",
                "name",
                "levelno",
                "levelname",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "getMessage",
                "message",
            }

            for key, value in record.__dict__.items():
                if key.startswith("_") or key in standard_fields:
                    continue
                extra[key] = redact_for_logging(value, key=key)

            loguru_logger.bind(**extra).opt(depth=6, exception=record.exc_info).log(
                level_to_use, record.getMessage()
            )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(lvl)
    root.addHandler(EnhancedInterceptHandler())

    # Reduce noise from verbose third-party loggers
    for noisy_logger in (
        "telethon",
        "telethon.network",
        "telethon.extensions",
    ):
        logging.getLogger(noisy_logger).setLevel(logging.INFO)

    # Log setup completion
    loguru_logger.info(
        "Enhanced JSON logging initialized with loguru",
        setup_config={
            "level": level,
            "include_location": include_location,
            "include_process_info": include_process_info,
            "log_file": log_file,
            "max_file_size": max_file_size,
            "retention": retention,
        },
    )


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance by name.

    This is a convenience wrapper around logging.getLogger() that provides
    consistent logger initialization across the application.

    Args:
        name: Logger name, typically __name__ of the calling module

    Returns:
        Logger instance for the given name
    """
    return logging.getLogger(name)


def log_exception(
    logger: logging.Logger,
    event: str,
    exc: BaseException,
    *,
    level: str = "error",
    **extra: Any,
) -> None:
    """Log an exception with structured context and traceback."""
    payload = {"error": redact_for_logging(str(exc)), "error_type": type(exc).__name__}
    payload.update(extra)

    if level == "warning":
        logger.warning(event, exc_info=exc, extra=payload)
    elif level == "info":
        logger.info(event, exc_info=exc, extra=payload)
    else:
        logger.error(event, exc_info=exc, extra=payload)


def generate_correlation_id() -> str:
    """Generate a short correlation ID for tracing errors across logs and user messages."""
    return uuid.uuid4().hex[:12]


_CORRELATION_ID_RE = re.compile(r"[A-Za-z0-9._:\-]{1,128}")


def sanitize_correlation_id(value: str | None) -> tuple[str, bool]:
    """Validate and return a safe correlation ID.

    Returns (id, was_generated) where was_generated=True means the incoming
    value was absent or invalid and a fresh ID was substituted.

    Allowed characters: A-Z a-z 0-9 . _ : -
    Max length: 128
    """
    if value and _CORRELATION_ID_RE.fullmatch(value):
        return value, False
    return f"api-{uuid.uuid4().hex[:16]}", True


def truncate_log_content(content: str | None, max_length: int = 1000) -> str | None:
    """Truncate large content for logging to avoid cluttering logs.

    Args:
        content: The content to potentially truncate
        max_length: Maximum length before truncation (default 1000)

    Returns:
        Truncated content with ellipsis if truncated, or original content if short enough

    """
    if not content:
        return content
    if len(content) <= max_length:
        return content

    # Smart truncation: try to break at word boundaries
    if max_length > 20:
        truncate_at = max_length - 15  # Leave space for ellipsis
        truncated = content[:truncate_at]

        # Find last space within reasonable distance
        last_space = truncated.rfind(" ", max(0, truncate_at - 50))
        if last_space > truncate_at - 100:
            truncated = truncated[:last_space]

        return truncated + "... [truncated]"

    return content[:max_length] + "..."


def redact_for_logging(
    value: Any,
    *,
    key: str | None = None,
    redact_urls: bool | None = None,
    allow_debug_content: bool = False,
    max_preview_chars: int = 200,
) -> Any:
    """Return a logging-safe copy of a value without secrets, prompts, raw content, or private URLs."""
    if redact_urls is None:
        redact_urls = _privacy_redact_urls_default()

    if key and key.lower() in _OPERATIONAL_KEY_EXACT:
        return value
    if key and (
        key.lower() in _SENSITIVE_KEY_EXACT
        or _SENSITIVE_KEY_RE.search(key)
        or _TOKEN_KEY_RE.search(key)
    ):
        return _REDACTED
    if key and _CONTENT_KEY_RE.search(key):
        if allow_debug_content or _DEBUG_PREVIEW_KEY_RE.search(key):
            return bounded_debug_preview(
                value, max_chars=max_preview_chars, redact_urls=redact_urls
            )
        return _CONTENT_REDACTED
    if key and redact_urls and _URL_KEY_RE.search(key):
        return redact_url_for_logging(value)

    if isinstance(value, dict):
        return {
            str(item_key): redact_for_logging(
                item_value,
                key=str(item_key),
                redact_urls=redact_urls,
                allow_debug_content=allow_debug_content,
                max_preview_chars=max_preview_chars,
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [
            redact_for_logging(
                item,
                redact_urls=redact_urls,
                allow_debug_content=allow_debug_content,
                max_preview_chars=max_preview_chars,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            redact_for_logging(
                item,
                redact_urls=redact_urls,
                allow_debug_content=allow_debug_content,
                max_preview_chars=max_preview_chars,
            )
            for item in value
        )
    if isinstance(value, str):
        return _redact_sensitive_text(value, redact_urls=redact_urls)
    return value


def bounded_debug_preview(
    value: Any,
    *,
    max_chars: int = 200,
    redact_urls: bool | None = None,
) -> str:
    """Build an explicitly requested bounded preview after token and URL redaction."""
    if redact_urls is None:
        redact_urls = _privacy_redact_urls_default()
    text = _redact_sensitive_text(str(value), redact_urls=redact_urls)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 15)] + "... [truncated]"


def redact_headers_for_logging(headers: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of HTTP headers with credential-bearing fields redacted."""
    if not headers:
        return {}
    return {str(key): redact_for_logging(value, key=str(key)) for key, value in headers.items()}


def redact_url_for_logging(value: Any, *, max_length: int = 220) -> Any:
    """Remove private URL path, query, fragment, and credentials while preserving host-level utility."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return raw
    try:
        split = urlsplit(raw)
    except Exception:
        return _redact_sensitive_text(raw, redact_urls=False)[:max_length]
    if not split.scheme or not split.netloc:
        return _redact_sensitive_text(raw, redact_urls=False)[:max_length]
    host = split.hostname or ""
    netloc = host
    if split.port:
        netloc = f"{netloc}:{split.port}"
    safe_query = _redact_url_query(split.query)
    sanitized = urlunsplit((split.scheme, netloc, "/[redacted]", safe_query, ""))
    if len(sanitized) > max_length:
        return sanitized[: max_length - 15] + "... [truncated]"
    return sanitized


def _redact_url_query(query: str) -> str:
    if not query:
        return ""
    safe_pairs: list[tuple[str, str]] = []
    for key, _value in parse_qsl(query, keep_blank_values=True):
        if key.lower() in _URL_SECRET_QUERY_KEYS:
            safe_pairs.append((key, _REDACTED))
    return urlencode(safe_pairs)


def _redact_sensitive_text(text: str, *, redact_urls: bool) -> str:
    redacted = _BEARER_RE.sub(lambda match: f"{match.group(1)} {_REDACTED}", text)
    redacted = _AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)}: {_REDACTED}", redacted)
    redacted = _TOKEN_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}={_REDACTED}", redacted)
    redacted = _TELEGRAM_BOT_TOKEN_RE.sub(_REDACTED, redacted)
    redacted = _GITHUB_TOKEN_RE.sub(_REDACTED, redacted)
    redacted = _API_KEY_RE.sub(_REDACTED, redacted)
    if redact_urls:
        redacted = _URL_WITH_SECRET_RE.sub(
            lambda match: str(redact_url_for_logging(match.group(0))),
            redacted,
        )
    return redacted


def sanitize_messages_for_logging(
    messages: list[dict[str, Any]],
    *,
    content_limit: int = 1000,
    allow_debug_content: bool = False,
) -> list[dict[str, Any]]:
    """Return sanitized message copies safe for logs and persistence."""
    sanitized: list[dict[str, Any]] = []
    for message in messages:
        sanitized_message = dict(message)
        content = sanitized_message.get("content", "")
        if allow_debug_content:
            sanitized_message["content"] = bounded_debug_preview(
                content,
                max_chars=content_limit,
            )
        else:
            sanitized_message["content"] = redact_for_logging(content, key="content")
        sanitized.append(sanitized_message)
    return sanitized


# Export commonly used items
__all__ = [
    "bounded_debug_preview",
    "generate_correlation_id",
    "get_logger",
    "log_exception",
    "redact_for_logging",
    "redact_headers_for_logging",
    "redact_url_for_logging",
    "sanitize_correlation_id",
    "sanitize_messages_for_logging",
    "setup_json_logging",
    "truncate_log_content",
]
