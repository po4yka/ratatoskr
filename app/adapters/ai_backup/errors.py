"""Error taxonomy for the AI account backup subsystem."""

from __future__ import annotations

import enum


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


__all__ = [
    "AiBackupAuthExpiredError",
    "AiBackupError",
    "AiBackupErrorCategory",
]
