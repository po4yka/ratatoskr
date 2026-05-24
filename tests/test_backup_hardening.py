"""Unit tests for backup hardening: config, crypto, safety, restore pipeline."""

from __future__ import annotations

import io
import json
import zipfile
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr, ValidationError

from app.config.backup import BackupConfig
from app.db.models import (
    Collection,
    CollectionItem,
    Request,
    Summary,
    SummaryHighlight,
    SummaryTag,
    Tag,
    User,
)

# ---------------------------------------------------------------------------
# BackupConfig
# ---------------------------------------------------------------------------


class TestBackupConfig:
    def test_encryption_enabled_when_key_set(self) -> None:
        cfg = BackupConfig(encryption_key=SecretStr("placeholder_will_be_replaced"))
        assert cfg.is_encryption_enabled is True

    def test_encryption_not_enabled_without_key(self) -> None:
        cfg = BackupConfig()
        assert cfg.is_encryption_enabled is False

    def test_explicit_false_overrides_key(self) -> None:
        cfg = BackupConfig(
            encryption_key=SecretStr("placeholder_will_be_replaced"),
            encryption_enabled=False,
        )
        assert cfg.is_encryption_enabled is False

    def test_explicit_true_without_key_raises(self) -> None:
        with pytest.raises(ValidationError, match="BACKUP_ENCRYPTION_ENABLED"):
            BackupConfig(encryption_enabled=True)

    def test_default_safety_limits(self) -> None:
        cfg = BackupConfig()
        assert cfg.max_restore_bytes == 100 * 1024 * 1024
        assert cfg.max_zip_entries == 100
        assert cfg.max_compressed_bytes == 100 * 1024 * 1024
        assert cfg.max_decompressed_bytes == 500 * 1024 * 1024
        assert cfg.max_compression_ratio == 100.0


# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------

from cryptography.fernet import Fernet as _Fernet

_TEST_KEY = _Fernet.generate_key()  # bytes, valid Fernet key
_TEST_KEY_STR = _TEST_KEY.decode()  # str version
_OTHER_KEY = _Fernet.generate_key().decode()  # different key for wrong-key tests


class TestBackupCrypto:
    def test_roundtrip(self) -> None:
        from pydantic import SecretStr

        from app.infrastructure.persistence.backup_crypto import (
            decrypt_backup,
            encrypt_backup,
        )

        plaintext = b"hello backup world"
        ciphertext = encrypt_backup(plaintext, SecretStr(_TEST_KEY_STR))
        assert decrypt_backup(ciphertext, SecretStr(_TEST_KEY_STR)) == plaintext

    def test_wrong_key_raises(self) -> None:
        from pydantic import SecretStr

        from app.infrastructure.persistence.backup_crypto import (
            InvalidBackupCiphertextError,
            decrypt_backup,
            encrypt_backup,
        )

        ciphertext = encrypt_backup(b"data", SecretStr(_TEST_KEY_STR))
        with pytest.raises(InvalidBackupCiphertextError):
            decrypt_backup(ciphertext, SecretStr(_OTHER_KEY))

    def test_is_fernet_ciphertext_true(self) -> None:
        from pydantic import SecretStr

        from app.infrastructure.persistence.backup_crypto import (
            encrypt_backup,
            is_fernet_ciphertext,
        )

        ciphertext = encrypt_backup(b"data", SecretStr(_TEST_KEY_STR))
        assert is_fernet_ciphertext(ciphertext) is True

    def test_is_fernet_ciphertext_false_for_raw_zip(self) -> None:
        import io
        import zipfile

        from app.infrastructure.persistence.backup_crypto import is_fernet_ciphertext

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("dummy.txt", "hello")
        assert is_fernet_ciphertext(buf.getvalue()) is False


# ---------------------------------------------------------------------------
# ZIP safety
# ---------------------------------------------------------------------------

_LIMITS: dict[str, Any] = {
    "max_entries": 10,
    "max_compressed_bytes": 10 * 1024 * 1024,
    "max_decompressed_bytes": 1000,
    "max_ratio": 50.0,
}


def _one_entry_zip(filename: str = "file.txt", content: bytes = b"hello") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, content)
    return buf.getvalue()


class TestZipSafety:
    def test_valid_archive_passes(self) -> None:
        from app.infrastructure.persistence.backup_safety import validate_zip_safety

        validate_zip_safety(_one_entry_zip(), **_LIMITS)  # should not raise

    def test_empty_archive_rejected(self) -> None:
        from app.infrastructure.persistence.backup_safety import (
            ZipSafetyViolation,
            validate_zip_safety,
        )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            pass
        with pytest.raises(ZipSafetyViolation, match="no entries"):
            validate_zip_safety(buf.getvalue(), **_LIMITS)

    def test_too_many_entries_rejected(self) -> None:
        from app.infrastructure.persistence.backup_safety import (
            ZipSafetyViolation,
            validate_zip_safety,
        )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(11):
                zf.writestr(f"f{i}.txt", "x")
        with pytest.raises(ZipSafetyViolation, match="entries"):
            validate_zip_safety(buf.getvalue(), **_LIMITS)

    def test_oversized_decompressed_rejected(self) -> None:
        from app.infrastructure.persistence.backup_safety import (
            ZipSafetyViolation,
            validate_zip_safety,
        )

        # 1001 bytes > max_decompressed_bytes=1000
        with pytest.raises(ZipSafetyViolation, match="decompressed"):
            validate_zip_safety(_one_entry_zip(content=b"x" * 1001), **_LIMITS)

    def test_zip_bomb_ratio_rejected(self) -> None:
        from app.infrastructure.persistence.backup_safety import (
            ZipSafetyViolation,
            validate_zip_safety,
        )

        # "a" * 5000 with DEFLATE compresses to ~15 bytes → ratio ≈ 333 > max_ratio=50
        limits = {**_LIMITS, "max_decompressed_bytes": 10 * 1024 * 1024}
        with pytest.raises(ZipSafetyViolation, match="ratio"):
            validate_zip_safety(_one_entry_zip(content=b"a" * 5000), **limits)

    def test_path_traversal_rejected(self) -> None:
        from app.infrastructure.persistence.backup_safety import (
            ZipSafetyViolation,
            validate_zip_safety,
        )

        with pytest.raises(ZipSafetyViolation, match="traversal"):
            validate_zip_safety(_one_entry_zip(filename="../../evil.txt"), **_LIMITS)

    def test_absolute_path_rejected(self) -> None:
        from app.infrastructure.persistence.backup_safety import (
            ZipSafetyViolation,
            validate_zip_safety,
        )

        with pytest.raises(ZipSafetyViolation, match="absolute"):
            validate_zip_safety(_one_entry_zip(filename="/etc/passwd"), **_LIMITS)

    def test_backslash_absolute_path_rejected(self) -> None:
        from app.infrastructure.persistence.backup_safety import (
            ZipSafetyViolation,
            validate_zip_safety,
        )

        # Windows-style absolute path bypasses naive startswith("/") check
        with pytest.raises(ZipSafetyViolation, match="absolute"):
            validate_zip_safety(_one_entry_zip(filename="\\etc\\passwd"), **_LIMITS)

    def test_windows_drive_path_rejected(self) -> None:
        from app.infrastructure.persistence.backup_safety import (
            ZipSafetyViolation,
            validate_zip_safety,
        )

        with pytest.raises(ZipSafetyViolation, match="absolute"):
            validate_zip_safety(_one_entry_zip(filename="C:/Windows/system32/evil.dll"), **_LIMITS)

    def test_corrupt_zip_raises_violation(self) -> None:
        from app.infrastructure.persistence.backup_safety import (
            ZipSafetyViolation,
            validate_zip_safety,
        )

        with pytest.raises(ZipSafetyViolation, match="corrupt"):
            validate_zip_safety(b"not a zip", **_LIMITS)


# ---------------------------------------------------------------------------
# Restore pipeline (decrypt + safety + import)
# ---------------------------------------------------------------------------


def _minimal_backup_zip() -> bytes:
    """Minimal valid backup ZIP with empty data arrays."""
    manifest = {
        "version": "1.0",
        "user_id": 1,
        "created_at": "2024-01-01T00:00:00+00:00",
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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for name in (
            "requests",
            "summaries",
            "tags",
            "summary_tags",
            "collections",
            "collection_items",
            "highlights",
        ):
            zf.writestr(f"{name}.json", "[]")
    return buf.getvalue()


def _populated_backup_zip() -> bytes:
    """Backup ZIP with one row per restorable entity and linked old IDs."""
    payloads: dict[str, Any] = {
        "requests": [
            {
                "id": 10,
                "type": "url",
                "status": "completed",
                "input_url": "https://example.com/post",
                "normalized_url": "https://example.com/post",
                "dedupe_hash": "dedupe-1",
                "lang_detected": "en",
            }
        ],
        "summaries": [
            {
                "id": 20,
                "request_id": 10,
                "lang": "en",
                "json_payload": {"summary_250": "A useful summary"},
                "is_read": True,
                "is_deleted": False,
            }
        ],
        "tags": [{"id": 30, "name": "AI", "normalized_name": "ai", "color": "#fff"}],
        "summary_tags": [{"id": 40, "summary_id": 20, "tag_id": 30, "source": "manual"}],
        "collections": [
            {
                "id": 50,
                "name": "Reading list",
                "description": "Saved",
                "position": 2,
                "collection_type": "manual",
                "query_conditions_json": None,
                "query_match_mode": "all",
            }
        ],
        "collection_items": [{"id": 60, "collection_id": 50, "summary_id": 20, "position": 1}],
        "highlights": [
            {
                "id": 70,
                "summary_id": 20,
                "text": "important",
                "start_offset": 0,
                "end_offset": 9,
                "color": "yellow",
                "note": "remember",
            }
        ],
    }
    manifest = {
        "version": "1.0",
        "schema_version": "1.0",
        "user_id": 1,
        "created_at": "2024-01-01T00:00:00+00:00",
        "counts": {key: len(value) for key, value in payloads.items()},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for name, value in payloads.items():
            zf.writestr(f"{name}.json", json.dumps(value))
        zf.writestr("preferences.json", "{}")
    return buf.getvalue()


def _make_mock_db() -> MagicMock:
    """Minimal DB mock that satisfies async_restore_from_archive."""

    @asynccontextmanager
    async def fake_transaction():
        session = MagicMock()
        session.scalar = AsyncMock(return_value=None)
        session.execute = AsyncMock(return_value=MagicMock())
        session.flush = AsyncMock()
        yield session

    db = MagicMock()
    db.transaction = fake_transaction
    return db


def _make_restore_db() -> MagicMock:
    """Fake DB that records restore objects and assigns primary keys on add."""
    sessions: list[MagicMock] = []

    @asynccontextmanager
    async def fake_transaction():
        session = MagicMock()
        session.scalar = AsyncMock(return_value=None)
        session.execute = AsyncMock(return_value=MagicMock())
        session.flush = AsyncMock()
        session.added = []
        next_ids = {
            "Request": 100,
            "Summary": 200,
            "Tag": 300,
            "SummaryTag": 400,
            "Collection": 500,
            "CollectionItem": 600,
            "SummaryHighlight": 700,
        }

        def add(instance: Any) -> None:
            name = type(instance).__name__
            if getattr(instance, "id", None) is None and name in next_ids:
                instance.id = next_ids[name]
                next_ids[name] += 1
            session.added.append(instance)

        session.add.side_effect = add
        sessions.append(session)
        yield session

    db = MagicMock()
    db.transaction = fake_transaction
    db.sessions = sessions
    return db


class _ScalarRows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _ExecuteRows:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []

    def scalars(self) -> _ScalarRows:
        return _ScalarRows(self._rows)


def _make_backup_create_db() -> MagicMock:
    """Fake DB for archive creation with one populated row per exported file."""
    rows_by_table: dict[str, list[Any]] = {
        "requests": [
            Request(
                id=10,
                user_id=1,
                type="url",
                status="completed",
                input_url="https://example.com/post",
                normalized_url="https://example.com/post",
                dedupe_hash="dedupe-1",
                lang_detected="en",
            )
        ],
        "summaries": [
            Summary(
                id=20,
                request_id=10,
                lang="en",
                json_payload={"summary_250": "A useful summary"},
                is_deleted=False,
                is_read=True,
            )
        ],
        "tags": [Tag(id=30, user_id=1, name="AI", normalized_name="ai", color="#fff")],
        "summary_tags": [SummaryTag(id=40, summary_id=20, tag_id=30, source="manual")],
        "collections": [
            Collection(
                id=50,
                user_id=1,
                name="Reading list",
                description="Saved",
                position=2,
                collection_type="manual",
                query_match_mode="all",
            )
        ],
        "collection_items": [CollectionItem(id=60, collection_id=50, summary_id=20, position=1)],
        "summary_highlights": [
            SummaryHighlight(
                id="00000000-0000-0000-0000-000000000070",
                user_id=1,
                summary_id=20,
                text="important",
                start_offset=0,
                end_offset=9,
                color="yellow",
                note="remember",
            )
        ],
        "user_backups": [],
    }
    sessions: list[MagicMock] = []

    @asynccontextmanager
    async def fake_transaction():
        session = MagicMock()
        session.executed = []
        session.get = AsyncMock(
            return_value=User(telegram_user_id=1, preferences_json={"backup_retention_count": 1})
        )
        sessions.append(session)

        async def execute(statement: Any) -> _ExecuteRows:
            session.executed.append(statement)
            statement_text = str(statement)
            for table_name, rows in rows_by_table.items():
                if f"FROM {table_name}" in statement_text:
                    return _ExecuteRows(rows)
            return _ExecuteRows()

        session.execute = AsyncMock(side_effect=execute)
        yield session

    db = MagicMock()
    db.transaction = fake_transaction
    db.sessions = sessions
    return db


class TestRestoreHardening:
    async def test_restore_accepts_encrypted_archive(self) -> None:
        from app.infrastructure.persistence.backup_archive_service import (
            async_restore_from_archive,
        )
        from app.infrastructure.persistence.backup_crypto import encrypt_backup

        encrypted = encrypt_backup(_minimal_backup_zip(), SecretStr(_TEST_KEY_STR))
        cfg = BackupConfig(encryption_key=SecretStr(_TEST_KEY_STR))
        result = await async_restore_from_archive(1, encrypted, db=_make_mock_db(), cfg=cfg)
        assert result["errors"] == []
        assert result["restored"]["requests"] == 0

    async def test_restore_accepts_unencrypted_archive(self) -> None:
        from app.infrastructure.persistence.backup_archive_service import (
            async_restore_from_archive,
        )

        cfg = BackupConfig()
        result = await async_restore_from_archive(
            1, _minimal_backup_zip(), db=_make_mock_db(), cfg=cfg
        )
        assert result["errors"] == []

    async def test_restore_rejects_encrypted_without_key(self) -> None:
        from app.infrastructure.persistence.backup_archive_service import (
            async_restore_from_archive,
        )
        from app.infrastructure.persistence.backup_crypto import encrypt_backup

        encrypted = encrypt_backup(_minimal_backup_zip(), SecretStr(_TEST_KEY_STR))
        cfg = BackupConfig()  # no key
        result = await async_restore_from_archive(1, encrypted, cfg=cfg)
        assert any("BACKUP_ENCRYPTION_KEY" in e for e in result["errors"])

    async def test_restore_rejects_wrong_key(self) -> None:
        from app.infrastructure.persistence.backup_archive_service import (
            async_restore_from_archive,
        )
        from app.infrastructure.persistence.backup_crypto import encrypt_backup

        encrypted = encrypt_backup(_minimal_backup_zip(), SecretStr(_TEST_KEY_STR))
        cfg = BackupConfig(encryption_key=SecretStr(_OTHER_KEY))
        result = await async_restore_from_archive(1, encrypted, cfg=cfg)
        assert any("decrypt" in e.lower() for e in result["errors"])

    async def test_restore_rejects_safety_violation(self) -> None:
        from app.infrastructure.persistence.backup_archive_service import (
            async_restore_from_archive,
        )

        # "a" * 5000 compresses to ~15 B → ratio ~333, exceeds default max_ratio=100
        bomb = _one_entry_zip(content=b"a" * 5000)
        cfg = BackupConfig()
        result = await async_restore_from_archive(1, bomb, cfg=cfg)
        assert len(result["errors"]) == 1
        assert "ratio" in result["errors"][0].lower()

    async def test_restore_imports_rows_and_remaps_dependent_ids(self) -> None:
        from app.infrastructure.persistence.backup_archive_service import (
            async_restore_from_archive,
        )

        db = _make_restore_db()
        result = await async_restore_from_archive(
            1,
            _populated_backup_zip(),
            db=db,
            cfg=BackupConfig(),
        )

        assert result["errors"] == []
        assert result["restored"] == {
            "requests": 1,
            "summaries": 1,
            "tags": 1,
            "summary_tags": 1,
            "collections": 1,
            "collection_items": 1,
            "highlights": 1,
        }

        added_by_type = {type(instance).__name__: instance for instance in db.sessions[0].added}
        assert added_by_type["Summary"].request_id == 100
        assert added_by_type["SummaryTag"].summary_id == 200
        assert added_by_type["SummaryTag"].tag_id == 300
        assert added_by_type["CollectionItem"].collection_id == 500
        assert added_by_type["CollectionItem"].summary_id == 200
        assert added_by_type["SummaryHighlight"].summary_id == 200

    async def test_create_backup_archive_exports_user_data_and_updates_metadata(
        self, tmp_path
    ) -> None:
        from app.infrastructure.persistence.backup_archive_service import (
            async_create_backup_archive,
            verify_backup_archive,
        )

        db = _make_backup_create_db()
        await async_create_backup_archive(
            1,
            99,
            db=db,
            data_dir=str(tmp_path),
            cfg=BackupConfig(),
        )

        backup_files = list((tmp_path / "backups" / "1").glob("ratatoskr-backup-1-*.zip"))
        assert len(backup_files) == 1
        payload = backup_files[0].read_bytes()
        verification = verify_backup_archive(payload, cfg=BackupConfig())
        assert verification["verification_status"] == "verified"
        assert verification["item_counts"] == {
            "requests": 1,
            "summaries": 1,
            "tags": 1,
            "summary_tags": 1,
            "collections": 1,
            "collection_items": 1,
            "highlights": 1,
        }

        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            assert json.loads(archive.read("preferences.json")) == {"backup_retention_count": 1}
            assert json.loads(archive.read("requests.json"))[0]["dedupe_hash"] == "dedupe-1"
            assert json.loads(archive.read("summaries.json"))[0]["json_payload"] == {
                "summary_250": "A useful summary"
            }

        assert len(db.sessions) == 3
        completed_update = str(db.sessions[1].executed[0])
        assert "file_size_bytes" in completed_update
        assert "verification_status" in completed_update


# ---------------------------------------------------------------------------
# Upload cap (router helper)
# ---------------------------------------------------------------------------


class TestUploadCap:
    async def test_oversized_upload_rejected(self) -> None:
        from app.api.exceptions import APIException
        from app.api.routers.backups import _read_upload_capped

        mock_file = AsyncMock()
        # 50 + 60 = 110 bytes > limit of 100
        mock_file.read.side_effect = [b"a" * 50, b"b" * 60, b""]
        with pytest.raises(APIException) as exc_info:
            await _read_upload_capped(mock_file, limit=100)
        assert exc_info.value.status_code == 413

    async def test_within_limit_passes(self) -> None:
        from app.api.routers.backups import _read_upload_capped

        mock_file = AsyncMock()
        mock_file.read.side_effect = [b"hello world", b""]
        content = await _read_upload_capped(mock_file, limit=100)
        assert content == b"hello world"
