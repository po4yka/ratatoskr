"""Error categorization for git sync operations.

Port of ``ErrorCategory.kt``. ``classify`` is order-sensitive: HTTP/2 first, then
connection-timed-out (NETWORK) ahead of generic timeout, then network, rate-limit,
auth, ssl, storage, repository, and finally UNKNOWN.
"""

from __future__ import annotations

from enum import Enum


class ErrorCategory(Enum):
    """Categories of errors that can occur during git sync operations."""

    HTTP2_ERROR = "HTTP2_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    TIMEOUT = "TIMEOUT"
    AUTH_ERROR = "AUTH_ERROR"
    REPOSITORY_ERROR = "REPOSITORY_ERROR"
    STORAGE_ERROR = "STORAGE_ERROR"
    SSL_ERROR = "SSL_ERROR"
    RATE_LIMIT = "RATE_LIMIT"
    UNKNOWN = "UNKNOWN"


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def classify(error_message: str | None) -> ErrorCategory:
    """Classify an error from its message. Returns UNKNOWN for ``None``."""
    if error_message is None:
        return ErrorCategory.UNKNOWN

    lower = error_message.lower()

    # HTTP/2 errors - highest priority for detection.
    if (
        "http/2" in lower
        or "http2" in lower
        or "curl 92" in lower
        or "curl 16" in lower
        or "curl 56" in lower
        or ("stream" in lower and "cancel" in lower)
    ):
        return ErrorCategory.HTTP2_ERROR

    # Connection timeout is a network error - check before generic timeout.
    if "connection timed out" in lower:
        return ErrorCategory.NETWORK_ERROR

    # Generic process/operation timeout.
    if "timeout" in lower or "timed out" in lower:
        return ErrorCategory.TIMEOUT

    if _contains_any(
        lower,
        (
            "connection reset",
            "connection refused",
            "network is unreachable",
            "host is unreachable",
            "recv failure",
            "couldn't connect",
            "could not connect",
            "failed to connect",
            "the remote end hung up unexpectedly",
            "broken pipe",
            "name or service not known",
            "temporary failure in name resolution",
            "could not resolve host",
            "remote: internal server error",
            "service unavailable",
            "early eof",
            "unexpected disconnect",
            "fetch-pack",
        ),
    ):
        return ErrorCategory.NETWORK_ERROR

    # Rate limiting - require surrounding context to avoid matching repo names/URLs.
    if _contains_any(
        lower,
        ("rate limit", "too many requests", "retry after", "error: 429", "returned error: 429"),
    ):
        return ErrorCategory.RATE_LIMIT

    # Authentication errors - "403" requires error context.
    if _contains_any(
        lower,
        (
            "authentication failed",
            "permission denied",
            "access denied",
            "invalid credentials",
            "bad credentials",
            "could not read username",
            "terminal prompts disabled",
            "error: 403",
            "returned error: 403",
            "repository not found",
        ),
    ):
        return ErrorCategory.AUTH_ERROR

    # SSL/TLS errors - specific patterns to avoid false positives from repo names.
    if _contains_any(
        lower,
        (
            "ssl certificate",
            "certificate problem",
            "certificate verify",
            "local issuer certificate",
            "ssl_error",
            "ssl: ",
            "tlsv1",
            "tls handshake",
            "tls alert",
            "gnutls_handshake",
        ),
    ):
        return ErrorCategory.SSL_ERROR

    # Storage errors (disk space, filesystem I/O, mount issues).
    if _contains_any(
        lower,
        (
            "no space left",
            "disk quota",
            "cannot allocate",
            "out of memory",
            "i/o error",
            "input/output error",
            "read-only file system",
            "structure needs cleaning",
            "stale file handle",
        ),
    ):
        return ErrorCategory.STORAGE_ERROR

    # Repository errors (non-retryable - manual intervention or config change).
    if _contains_any(
        lower,
        (
            "repository is empty",
            "remote head",
            "nonexistent ref",
            "invalid ref",
            "couldn't find remote ref",
            "remote ref does not exist",
            "bad object",
            "is corrupt",
            "does not appear to be a git",
            "404 not found",
        ),
    ):
        return ErrorCategory.REPOSITORY_ERROR

    return ErrorCategory.UNKNOWN


_HTTP1_FALLBACK = frozenset({ErrorCategory.HTTP2_ERROR, ErrorCategory.NETWORK_ERROR})

_NON_RETRYABLE = frozenset(
    {
        ErrorCategory.AUTH_ERROR,
        ErrorCategory.STORAGE_ERROR,
        ErrorCategory.SSL_ERROR,
        ErrorCategory.REPOSITORY_ERROR,
    }
)

_DELAY_MULTIPLIER = {
    ErrorCategory.HTTP2_ERROR: 1.0,
    ErrorCategory.NETWORK_ERROR: 2.0,
    ErrorCategory.TIMEOUT: 1.5,
    ErrorCategory.RATE_LIMIT: 3.0,
}

_DISPLAY_NAME = {
    ErrorCategory.HTTP2_ERROR: "HTTP/2 Error",
    ErrorCategory.NETWORK_ERROR: "Network Error",
    ErrorCategory.TIMEOUT: "Timeout",
    ErrorCategory.AUTH_ERROR: "Authentication Error",
    ErrorCategory.REPOSITORY_ERROR: "Git Error",
    ErrorCategory.STORAGE_ERROR: "Disk Space Error",
    ErrorCategory.SSL_ERROR: "SSL/TLS Error",
    ErrorCategory.RATE_LIMIT: "Rate Limiting",
    ErrorCategory.UNKNOWN: "Unknown Error",
}

_SUGGESTION = {
    ErrorCategory.HTTP2_ERROR: (
        "Retry with HTTP/1.1 fallback is automatic. If persistent, check network proxy settings."
    ),
    ErrorCategory.NETWORK_ERROR: (
        "Check your internet connection and DNS settings. "
        "Verify the repository URL is accessible."
    ),
    ErrorCategory.TIMEOUT: (
        "Operation timed out. The repository may be large or the connection slow. "
        "Consider increasing timeout."
    ),
    ErrorCategory.AUTH_ERROR: (
        "Verify your credentials and token permissions. Ensure the token hasn't expired."
    ),
    ErrorCategory.REPOSITORY_ERROR: (
        "Verify the repository exists and the URL is correct. "
        "Check if it has been deleted, moved, or emptied."
    ),
    ErrorCategory.STORAGE_ERROR: (
        "Free up disk space on your system. Consider archiving or removing old backups. "
        "Verify the backup volume is mounted and the filesystem is not in read-only or error "
        "state. Check `dmesg` for I/O errors."
    ),
    ErrorCategory.SSL_ERROR: (
        "Check SSL certificate configuration. Verify system certificates are up to date or "
        "configure cert_file in config."
    ),
    ErrorCategory.RATE_LIMIT: (
        "Wait before retrying. Consider reducing sync frequency or using authentication to "
        "increase rate limits."
    ),
    ErrorCategory.UNKNOWN: (
        "Check the error message for details. Verify your configuration and network connectivity."
    ),
}


_PERMANENTLY_GONE_NEEDLES: tuple[str, ...] = (
    "repository not found",
    "could not find repository",
    "does not exist",
    "returned error: 404",
    "returned error: 410",
    "error: 404",
    "error: 410",
    " 404 ",
    " 410 ",
)

# Signals that must NOT trigger tombstoning even if a gone-pattern appears in
# the same message.  Auth errors, credential problems, and permission denials
# indicate transient or operator-fixable states, not permanent absence.
_NOT_PERMANENTLY_GONE_NEEDLES: tuple[str, ...] = (
    "authentication failed",
    "invalid credentials",
    "bad credentials",
    "permission denied",
    "access denied",
    "could not read username",
    "terminal prompts disabled",
    "403",
)


def is_permanently_gone(message: str | None) -> bool:
    """Return True when *message* signals that the repository is permanently gone.

    Conservative: only matches signals that unambiguously mean the remote repo
    has been deleted, renamed, or is otherwise gone-by-identity (HTTP 404/410,
    "repository not found", "does not exist", "could not find repository").

    Auth errors (403, credential failures, permission denied), network errors,
    timeouts, SSL issues, and rate limits return False -- those are transient or
    operator-fixable and must keep using the normal cooldown path.

    ``None`` or empty string always returns False.
    """
    if not message:
        return False

    lower = message.lower()

    # If any auth/transient signal is present, never tombstone.
    if _contains_any(lower, _NOT_PERMANENTLY_GONE_NEEDLES):
        return False

    return _contains_any(lower, _PERMANENTLY_GONE_NEEDLES)


def should_use_http1_fallback(category: ErrorCategory) -> bool:
    """Whether this category should trigger an HTTP/1.1 fallback on retry."""
    return category in _HTTP1_FALLBACK


def is_retryable(category: ErrorCategory) -> bool:
    """Whether this category should be retried at all."""
    return category not in _NON_RETRYABLE


def delay_multiplier(category: ErrorCategory) -> float:
    """Delay multiplier applied to the backoff for this category."""
    return _DELAY_MULTIPLIER.get(category, 1.0)


def display_name(category: ErrorCategory) -> str:
    """Human-readable name for notifications."""
    return _DISPLAY_NAME[category]


def suggestion(category: ErrorCategory) -> str:
    """Actionable recovery suggestion for this category."""
    return _SUGGESTION[category]
