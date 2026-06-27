"""AI account backup adapter package (ChatGPT + Claude via CloakBrowser)."""

from __future__ import annotations

from app.adapters.ai_backup.errors import (
    AiBackupAuthExpiredError,
    AiBackupError,
    AiBackupErrorCategory,
    AiBackupHostDeniedError,
    AiBackupMaxRequestsError,
    AiBackupParseError,
    PathTraversalError,
    classify_error,
)
from app.adapters.ai_backup.repository import AiBackupRepository
from app.adapters.ai_backup.service import (
    AiBackupOrchestrationService,
    BackupNotifier,
    NullNotifier,
)
from app.adapters.ai_backup.session_store import AiBackupSessionStore

__all__ = [
    "AiBackupAuthExpiredError",
    "AiBackupError",
    "AiBackupErrorCategory",
    "AiBackupHostDeniedError",
    "AiBackupMaxRequestsError",
    "AiBackupOrchestrationService",
    "AiBackupParseError",
    "AiBackupRepository",
    "AiBackupSessionStore",
    "BackupNotifier",
    "NullNotifier",
    "PathTraversalError",
    "classify_error",
]
