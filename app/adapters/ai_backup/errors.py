"""Error taxonomy for the AI account backup subsystem."""

from __future__ import annotations

import asyncio
import enum
import json


class AiBackupErrorCategory(enum.StrEnum):
    """Coarse failure classification persisted on ``ai_account_backups.last_error_category``.

    ``AUTH_EXPIRED`` is special: it halts the service (no automatic retry) and
    is surfaced to the operator so they can re-supply a session. The others are
    transient and subject to the failure-backoff policy.
    """

    AUTH_EXPIRED = "auth_expired"
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"
    NETWORK = "network"
    PARSE = "parse"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


class AiBackupError(Exception):
    """Base error for the AI account backup subsystem."""


class AiBackupAuthExpiredError(AiBackupError):
    """Raised when the stored session is no longer valid and re-auth is required."""


class AiBackupHostDeniedError(AiBackupError):
    """Raised when a target URL's host is not in AI_BACKUP_HOST_ALLOWLIST."""


class AiBackupMaxRequestsError(AiBackupError):
    """Raised when the per-run request cap is exceeded. Not an auth failure."""


class AiBackupParseError(AiBackupError):
    """Raised when a remote response cannot be parsed (bad JSON, missing field)."""


class PathTraversalError(AiBackupError):
    """Raised when a remote-supplied id or filename resolves outside the data root."""


def classify_error(exc: BaseException) -> AiBackupErrorCategory:
    """Map a caught exception to the coarse category persisted on the lifecycle row.

    Checked most-specific first. httpx/Playwright errors are matched by module
    name so this module never has to import either optional dependency.
    """
    if isinstance(exc, AiBackupAuthExpiredError):
        return AiBackupErrorCategory.AUTH_EXPIRED
    if isinstance(exc, (AiBackupHostDeniedError, AiBackupMaxRequestsError)):
        return AiBackupErrorCategory.BLOCKED
    if isinstance(exc, AiBackupParseError):
        return AiBackupErrorCategory.PARSE
    if isinstance(exc, json.JSONDecodeError):
        return AiBackupErrorCategory.PARSE
    # asyncio.TimeoutError is the builtin TimeoutError (a subclass of OSError) on
    # 3.11+, so it is also covered by the OSError branch; listed for clarity.
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return AiBackupErrorCategory.NETWORK
    module = type(exc).__module__ or ""
    if module.startswith(("httpx", "playwright")):
        return AiBackupErrorCategory.NETWORK
    return AiBackupErrorCategory.UNKNOWN


__all__ = [
    "AiBackupAuthExpiredError",
    "AiBackupError",
    "AiBackupErrorCategory",
    "AiBackupHostDeniedError",
    "AiBackupMaxRequestsError",
    "AiBackupParseError",
    "PathTraversalError",
    "classify_error",
]
