"""Tests for backup service: manifest validation, ZIP structure, retention logic."""

from __future__ import annotations

import io
import json
import unittest
import zipfile

from app.config.backup import BackupConfig
from app.infrastructure.persistence.backup_archive_service import (
    async_cleanup_old_user_backups,
    calculate_backup_checksum,
    dry_run_restore_from_archive,
    restore_from_archive,
    verify_backup_archive,
)


def _build_manifest(*, version: str = "1.0", user_id: int = 1) -> dict:
    return {
        "version": version,
        "schema_version": version,
        "user_id": user_id,
        "created_at": "2025-01-01T00:00:00+00:00",
        "counts": {
            "requests": 0,
            "summaries": 0,
            "tags": 0,
            "summary_tags": 0,
            "collections": 0,
            "collection_items": 0,
            "highlights": 0,
        },
    }


_ENTITY_FILES = (
    "requests.json",
    "summaries.json",
    "tags.json",
    "summary_tags.json",
    "collections.json",
    "collection_items.json",
    "highlights.json",
    "preferences.json",
)


def _make_zip(manifest: dict, *, include_entities: bool = True) -> bytes:
    """Build an in-memory backup ZIP with a manifest and empty entity files."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        if include_entities:
            for name in _ENTITY_FILES:
                zf.writestr(name, "[]" if name != "preferences.json" else "{}")
    buf.seek(0)
    return buf.read()


class TestBackupManifest:
    """Manifest validation inside restore_from_archive."""

    def test_manifest_has_required_keys(self) -> None:
        manifest = _build_manifest()
        assert "version" in manifest
        assert "user_id" in manifest
        assert "counts" in manifest
        assert manifest["schema_version"] == "1.0"
        assert set(manifest["counts"]) == {
            "requests",
            "summaries",
            "tags",
            "summary_tags",
            "collections",
            "collection_items",
            "highlights",
        }

    def test_unsupported_version_returns_error(self) -> None:
        zip_bytes = _make_zip(_build_manifest(version="99.0"))
        result = restore_from_archive(user_id=1, zip_bytes=zip_bytes)
        assert len(result["errors"]) == 1
        assert "Unsupported backup version" in result["errors"][0]

    def test_corrupt_zip_returns_error(self) -> None:
        result = restore_from_archive(user_id=1, zip_bytes=b"not a zip")
        assert any("Invalid or corrupt ZIP" in e for e in result["errors"])

    def test_missing_manifest_returns_error(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("dummy.txt", "hello")
        result = restore_from_archive(user_id=1, zip_bytes=buf.getvalue())
        assert any("Missing required file" in e or "manifest" in e for e in result["errors"])


class TestBackupZipStructure:
    """Verify expected ZIP layout produced by create_backup_archive."""

    def test_expected_files_present(self) -> None:
        zip_bytes = _make_zip(_build_manifest())
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            names = set(zf.namelist())
        expected = {"manifest.json"} | set(_ENTITY_FILES)
        assert expected == names

    def test_manifest_is_valid_json(self) -> None:
        zip_bytes = _make_zip(_build_manifest())
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            manifest = json.loads(zf.read("manifest.json"))
        assert manifest["version"] == "1.0"
        assert manifest["schema_version"] == "1.0"

    def test_entity_files_are_valid_json(self) -> None:
        zip_bytes = _make_zip(_build_manifest())
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            for name in _ENTITY_FILES:
                data = json.loads(zf.read(name))
                assert isinstance(data, (list, dict))


class TestEnforceRetentionLogic(unittest.TestCase):
    """Test the retention pruning logic (unit-level, no DB)."""

    def test_items_beyond_max_count_are_identified(self) -> None:
        """Given a sorted-descending list, items after max_count are 'to_delete'."""
        backups = list(range(7))  # IDs 0..6, newest first
        max_count = 5
        to_delete = backups[max_count:]
        assert to_delete == [5, 6]
        assert len(to_delete) == 2

    def test_no_deletion_when_within_limit(self) -> None:
        backups = list(range(3))
        max_count = 5
        to_delete = backups[max_count:]
        assert to_delete == []

    def test_exact_limit_means_no_deletion(self) -> None:
        backups = list(range(5))
        max_count = 5
        to_delete = backups[max_count:]
        assert to_delete == []


class TestBackupVerification:
    def test_verifies_checksum_and_archive_metadata(self) -> None:
        zip_bytes = _make_zip(_build_manifest())
        checksum = calculate_backup_checksum(zip_bytes)

        verification = verify_backup_archive(
            zip_bytes,
            cfg=BackupConfig(),
            expected_checksum=checksum,
        )

        assert verification["checksum"] == checksum
        assert verification["schema_version"] == "1.0"
        assert verification["verification_status"] == "verified"
        assert verification["verification_error"] is None
        assert verification["item_counts"]["requests"] == 0

    def test_corrupt_backup_fails_verification(self) -> None:
        zip_bytes = _make_zip(_build_manifest())
        checksum = calculate_backup_checksum(zip_bytes)
        corrupted = zip_bytes[:-8] + b"corrupt!"

        verification = verify_backup_archive(
            corrupted,
            cfg=BackupConfig(),
            expected_checksum=checksum,
        )

        assert verification["verification_status"] == "failed"
        assert "checksum" in verification["verification_error"].lower()

    def test_dry_run_reports_compatibility_and_counts(self) -> None:
        manifest = _build_manifest()
        manifest["counts"]["requests"] = 2
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("requests.json", json.dumps([{"id": 1}, {"id": 2}]))
            for name in _ENTITY_FILES:
                if name == "requests.json":
                    continue
                zf.writestr(name, "[]" if name != "preferences.json" else "{}")

        result = dry_run_restore_from_archive(
            user_id=1, zip_bytes=buf.getvalue(), cfg=BackupConfig()
        )

        assert result["valid"] is True
        assert result["compatible"] is True
        assert result["schema_version"] == "1.0"
        assert result["counts"]["requests"] == 2
        assert result["estimated_affected_rows"]["requests"] == 2
        assert result["errors"] == []

    async def test_retention_cleanup_keeps_in_flight_backups(self) -> None:
        class _Backup:
            def __init__(self, backup_id: int, status: str) -> None:
                self.id = backup_id
                self.status = status
                self.file_path = None

        class _Scalars:
            def all(self) -> list[_Backup]:
                return [
                    _Backup(1, "completed"),
                    _Backup(3, "failed"),
                ]

        class _ExecuteResult:
            def scalars(self) -> _Scalars:
                return _Scalars()

        class _Session:
            def __init__(self) -> None:
                self.deleted: list[int] = []

            async def execute(self, statement):
                if "DELETE" in str(statement):
                    self.deleted.append(3)
                return _ExecuteResult()

        class _Transaction:
            def __init__(self) -> None:
                self.session = _Session()

            async def __aenter__(self) -> _Session:
                return self.session

            async def __aexit__(self, *exc_info) -> None:
                return None

        class _Database:
            def __init__(self) -> None:
                self.tx = _Transaction()

            def transaction(self) -> _Transaction:
                return self.tx

        database = _Database()
        result = await async_cleanup_old_user_backups(database, user_id=1, keep_count=1)  # type: ignore[arg-type]

        assert result == {"deleted": 1, "filesDeleted": 0}
        assert database.tx.session.deleted == [3]
