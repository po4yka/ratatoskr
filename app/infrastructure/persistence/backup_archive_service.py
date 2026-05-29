"""PostgreSQL-backed backup archive workflows -- thin facade.

All implementation lives in focused modules:
    backup_writer.py    -- archive creation, retention pruning, shared constants/helpers
    backup_inspector.py -- inspection, verification (read-only, no DB writes)
    backup_reader.py    -- full restore and dry-run restore
    backup_crypto.py    -- Fernet encryption/decryption
    backup_safety.py    -- ZIP safety validation

This module re-exports the complete public surface so existing callers are unaffected.
"""

from __future__ import annotations

from app.infrastructure.persistence.backup_inspector import (
    BackupArchiveInspection,
    inspect_backup_archive,
    verify_backup_archive,
)
from app.infrastructure.persistence.backup_reader import (
    async_dry_run_restore_from_archive,
    async_restore_from_archive,
    dry_run_restore_from_archive,
    restore_from_archive,
)
from app.infrastructure.persistence.backup_writer import (
    BACKUP_SCHEMA_VERSION,
    async_cleanup_old_user_backups,
    async_create_backup_archive,
    calculate_backup_checksum,
    create_backup_archive,
)

__all__ = [
    "BACKUP_SCHEMA_VERSION",
    "BackupArchiveInspection",
    "async_cleanup_old_user_backups",
    "async_create_backup_archive",
    "async_dry_run_restore_from_archive",
    "async_restore_from_archive",
    "calculate_backup_checksum",
    "create_backup_archive",
    "dry_run_restore_from_archive",
    "inspect_backup_archive",
    "restore_from_archive",
    "verify_backup_archive",
]
