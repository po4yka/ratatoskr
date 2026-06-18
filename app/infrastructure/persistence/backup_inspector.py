"""Backup archive inspection and verification (read-only, no DB writes).

Public surface (re-exported by backup_archive_service):
    BackupArchiveInspection
    inspect_backup_archive
    verify_backup_archive
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.infrastructure.persistence.backup_crypto import (
    InvalidBackupCiphertextError,
    decrypt_backup,
    decrypt_backup_stream,
    is_fernet_ciphertext,
    is_streaming_ciphertext,
)
from app.infrastructure.persistence.backup_safety import ZipSafetyViolation, validate_zip_safety
from app.infrastructure.persistence.backup_writer import (
    _ENTITY_FILE_BY_COUNT_KEY,
    _REQUIRED_FILES,
    BACKUP_SCHEMA_VERSION,
    _read_json,
    calculate_backup_checksum,
)

if TYPE_CHECKING:
    from app.config.backup import BackupConfig

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackupArchiveInspection:
    """Validated archive metadata used by verification and dry-run restore."""

    manifest: dict[str, Any]
    counts: dict[str, int]
    schema_version: str
    created_at: str | None
    encrypted: bool


# ---------------------------------------------------------------------------
# Pure helpers shared with backup_reader
# ---------------------------------------------------------------------------


def _empty_restore_counts() -> dict[str, int]:
    return dict.fromkeys(_ENTITY_FILE_BY_COUNT_KEY, 0)


def _decrypt_archive_payload(
    payload: bytes,
    cfg: BackupConfig,
    *,
    errors: list[str],
) -> tuple[bytes | None, bool]:
    if is_streaming_ciphertext(payload):
        if cfg.encryption_key is None:
            errors.append("Encrypted backup but BACKUP_ENCRYPTION_KEY is not configured")
            return None, True
        try:
            import io

            src = io.BytesIO(payload)
            dst = io.BytesIO()
            decrypt_backup_stream(src, dst, cfg.encryption_key)
            return dst.getvalue(), True
        except InvalidBackupCiphertextError:
            errors.append("Could not decrypt backup (wrong key or corrupted archive)")
            return None, True
    if is_fernet_ciphertext(payload):
        if cfg.encryption_key is None:
            errors.append("Encrypted backup but BACKUP_ENCRYPTION_KEY is not configured")
            return None, True
        try:
            return decrypt_backup(payload, cfg.encryption_key), True
        except InvalidBackupCiphertextError:
            errors.append("Could not decrypt backup (wrong key or corrupted archive)")
            return None, True
    return payload, False


def _validate_payload_safety(payload: bytes, cfg: BackupConfig, *, errors: list[str]) -> bool:
    try:
        validate_zip_safety(
            payload,
            max_entries=cfg.max_zip_entries,
            max_compressed_bytes=cfg.max_compressed_bytes,
            max_decompressed_bytes=cfg.max_decompressed_bytes,
            max_ratio=cfg.max_compression_ratio,
        )
    except ZipSafetyViolation as exc:
        errors.append(str(exc))
        return False
    return True


# ---------------------------------------------------------------------------
# Inspection / verification
# ---------------------------------------------------------------------------


def inspect_backup_archive(
    payload: bytes,
    *,
    cfg: BackupConfig,
    expected_checksum: str | None = None,
) -> tuple[BackupArchiveInspection | None, list[str]]:
    """Validate a backup payload and return manifest/count metadata."""
    errors: list[str] = []
    checksum = calculate_backup_checksum(payload)
    if expected_checksum is not None and checksum != expected_checksum:
        errors.append("Backup checksum mismatch")

    zip_bytes, encrypted = _decrypt_archive_payload(payload, cfg, errors=errors)
    if zip_bytes is None:
        return None, errors
    if not encrypted:
        logger.warning("backup_archive_unencrypted")

    if not _validate_payload_safety(zip_bytes, cfg, errors=errors):
        return None, errors

    try:
        with zipfile.ZipFile(BytesIO(zip_bytes), "r") as archive:
            names = set(archive.namelist())
            missing = sorted(_REQUIRED_FILES - names)
            if missing:
                errors.append(f"Missing required file in backup archive: {', '.join(missing)}")
                return None, errors

            manifest = _read_json(archive, "manifest.json")
            if not isinstance(manifest, dict):
                errors.append("Backup manifest must be a JSON object")
                return None, errors

            schema_version = str(
                manifest.get("schema_version") or manifest.get("version") or "unknown"
            )
            if schema_version != BACKUP_SCHEMA_VERSION:
                errors.append(f"Unsupported backup schema version: {schema_version}")

            manifest_counts = manifest.get("counts")
            if not isinstance(manifest_counts, dict):
                errors.append("Backup manifest counts must be a JSON object")
                return None, errors

            actual_counts: dict[str, int] = {}
            for key, filename in _ENTITY_FILE_BY_COUNT_KEY.items():
                data = _read_json(archive, filename)
                if not isinstance(data, list):
                    errors.append(f"{filename} must contain a JSON array")
                    continue
                actual_counts[key] = len(data)
                expected = manifest_counts.get(key)
                if expected is not None and int(expected) != len(data):
                    errors.append(
                        f"Manifest count mismatch for {key}: expected {expected}, found {len(data)}"
                    )

            preferences = _read_json(archive, "preferences.json")
            if not isinstance(preferences, dict):
                errors.append("preferences.json must contain a JSON object")

            inspection = BackupArchiveInspection(
                manifest=manifest,
                counts=actual_counts,
                schema_version=schema_version,
                created_at=manifest.get("created_at"),
                encrypted=encrypted,
            )
            return inspection, errors
    except KeyError as exc:
        errors.append(f"Missing required file in backup archive: {exc}")
    except zipfile.BadZipFile:
        errors.append("Invalid or corrupt ZIP archive")
    except json.JSONDecodeError as exc:
        errors.append(f"Invalid backup JSON: {exc}")
    except Exception as exc:
        errors.append(str(exc))
    return None, errors


def verify_backup_archive(
    payload: bytes,
    *,
    cfg: BackupConfig,
    expected_checksum: str | None = None,
) -> dict[str, Any]:
    """Return verification metadata for a stored backup payload."""
    inspection, errors = inspect_backup_archive(
        payload,
        cfg=cfg,
        expected_checksum=expected_checksum,
    )
    return {
        "checksum": calculate_backup_checksum(payload),
        "item_counts": inspection.counts if inspection else {},
        "schema_version": inspection.schema_version if inspection else None,
        "created_at": inspection.created_at if inspection else None,
        "verified_at": datetime.now(UTC).isoformat(),
        "verification_status": "failed" if errors else "verified",
        "verification_error": "; ".join(errors) if errors else None,
    }
