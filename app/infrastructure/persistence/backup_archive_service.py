"""PostgreSQL-backed backup archive workflows."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import zipfile
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, select, update

from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.db.models import (
    Collection,
    CollectionItem,
    Request,
    Summary,
    SummaryHighlight,
    SummaryTag,
    Tag,
    User,
    UserBackup,
    model_to_dict,
)
from app.db.types import _utcnow
from app.infrastructure.persistence.backup_crypto import (
    InvalidBackupCiphertextError,
    decrypt_backup,
    encrypt_backup,
    is_fernet_ciphertext,
)
from app.infrastructure.persistence.backup_safety import ZipSafetyViolation, validate_zip_safety

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.config.backup import BackupConfig
    from app.db.session import Database

logger = get_logger(__name__)

BACKUP_SCHEMA_VERSION = "1.0"
_ENTITY_FILE_BY_COUNT_KEY = {
    "requests": "requests.json",
    "summaries": "summaries.json",
    "tags": "tags.json",
    "summary_tags": "summary_tags.json",
    "collections": "collections.json",
    "collection_items": "collection_items.json",
    "highlights": "highlights.json",
}
_REQUIRED_FILES = {"manifest.json", "preferences.json", *_ENTITY_FILE_BY_COUNT_KEY.values()}


@dataclass(frozen=True, slots=True)
class BackupArchiveInspection:
    """Validated archive metadata used by verification and dry-run restore."""

    manifest: dict[str, Any]
    counts: dict[str, int]
    schema_version: str
    created_at: str | None
    encrypted: bool


def _database(db: Database | None) -> Database:
    if db is not None:
        return db

    from app.api.dependencies.database import get_session_manager

    return get_session_manager()


def _resolve_data_dir(data_dir: str | None) -> Path:
    return Path(data_dir or os.getenv("DATA_DIR", "/data"))


def _coerce_retention_count(preferences: Any) -> int | None:
    if not isinstance(preferences, dict):
        return None
    raw_value = preferences.get("backup_retention_count")
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _dump_rows(rows: Sequence[object]) -> list[dict[str, Any]]:
    return [row for row in (model_to_dict(item) for item in rows) if row is not None]


def _read_json(archive: zipfile.ZipFile, name: str) -> Any:
    return json.loads(archive.read(name))


def calculate_backup_checksum(payload: bytes) -> str:
    """Return the SHA-256 checksum for the exact stored backup payload bytes."""
    return hashlib.sha256(payload).hexdigest()


def _empty_restore_counts() -> dict[str, int]:
    return dict.fromkeys(_ENTITY_FILE_BY_COUNT_KEY, 0)


def _empty_restore_summary() -> dict[str, dict[str, int] | list[str]]:
    return {
        "restored": _empty_restore_counts(),
        "skipped": {
            "requests": 0,
            "summaries": 0,
            "tags": 0,
            "collections": 0,
        },
        "errors": [],
    }


def _decrypt_archive_payload(
    payload: bytes,
    cfg: BackupConfig,
    *,
    errors: list[str],
) -> tuple[bytes | None, bool]:
    encrypted = is_fernet_ciphertext(payload)
    if encrypted:
        if cfg.encryption_key is None:
            errors.append("Encrypted backup but BACKUP_ENCRYPTION_KEY is not configured")
            return None, encrypted
        try:
            return decrypt_backup(payload, cfg.encryption_key), encrypted
        except InvalidBackupCiphertextError:
            errors.append("Could not decrypt backup (wrong key or corrupted archive)")
            return None, encrypted
    return payload, encrypted


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


def _old_id(row: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return int(value)
    return None


async def async_cleanup_old_user_backups(
    database: Database,
    *,
    user_id: int,
    keep_count: int,
) -> dict[str, int]:
    """Prune old terminal backup records while keeping in-flight backups untouched."""
    if keep_count <= 0:
        return {"deleted": 0, "filesDeleted": 0}

    async with database.transaction() as session:
        rows = list(
            (
                await session.execute(
                    select(UserBackup)
                    .where(
                        UserBackup.user_id == user_id,
                        UserBackup.status.in_(("completed", "failed")),
                    )
                    .order_by(UserBackup.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        obsolete = rows[keep_count:]
        files_deleted = 0
        for backup in obsolete:
            file_path = backup.file_path
            if file_path:
                path = Path(file_path)
                if path.is_file() and path.name.startswith("ratatoskr-backup-"):
                    path.unlink()
                    files_deleted += 1
            await session.execute(delete(UserBackup).where(UserBackup.id == backup.id))
        return {"deleted": len(obsolete), "filesDeleted": files_deleted}


async def async_create_backup_archive(
    user_id: int,
    backup_id: int,
    *,
    db: Database | None = None,
    data_dir: str | None = None,
    cfg: BackupConfig | None = None,
) -> None:
    """Create a ZIP backup of all user data."""
    from app.config.backup import load_backup_config

    cfg = cfg or load_backup_config()
    database = _database(db)
    backup_dir = _resolve_data_dir(data_dir) / "backups" / str(user_id)

    try:
        async with database.transaction() as session:
            await session.execute(
                update(UserBackup)
                .where(UserBackup.id == backup_id)
                .values(status="processing", updated_at=_utcnow())
            )

            user_row = await session.get(User, user_id)
            if user_row is None:
                msg = f"User {user_id} not found"
                raise ValueError(msg)
            preferences = user_row.preferences_json

            requests_rows = list(
                (
                    await session.execute(
                        select(Request)
                        .where(Request.user_id == user_id)
                        .order_by(Request.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            requests_data = _dump_rows(requests_rows)

            summaries_rows = list(
                (
                    await session.execute(
                        select(Summary)
                        .join(Request, Summary.request_id == Request.id)
                        .where(Request.user_id == user_id, Summary.is_deleted.is_(False))
                    )
                )
                .scalars()
                .all()
            )
            summary_ids = [summary.id for summary in summaries_rows]
            summaries_data = _dump_rows(summaries_rows)

            tags_rows = list(
                (
                    await session.execute(
                        select(Tag).where(Tag.user_id == user_id, Tag.is_deleted.is_(False))
                    )
                )
                .scalars()
                .all()
            )
            tag_ids = [tag.id for tag in tags_rows]
            tags_data = _dump_rows(tags_rows)

            summary_tags_rows = (
                list(
                    (
                        await session.execute(
                            select(SummaryTag).where(
                                SummaryTag.summary_id.in_(summary_ids),
                                SummaryTag.tag_id.in_(tag_ids),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if summary_ids and tag_ids
                else []
            )
            summary_tags_data = _dump_rows(summary_tags_rows)

            collections_rows = list(
                (
                    await session.execute(
                        select(Collection).where(
                            Collection.user_id == user_id,
                            Collection.is_deleted.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            collection_ids = [collection.id for collection in collections_rows]
            collections_data = _dump_rows(collections_rows)

            collection_items_rows = (
                list(
                    (
                        await session.execute(
                            select(CollectionItem).where(
                                CollectionItem.collection_id.in_(collection_ids)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if collection_ids
                else []
            )
            collection_items_data = _dump_rows(collection_items_rows)

            highlights_rows = list(
                (
                    await session.execute(
                        select(SummaryHighlight).where(SummaryHighlight.user_id == user_id)
                    )
                )
                .scalars()
                .all()
            )
            highlights_data = _dump_rows(highlights_rows)

            items_count = (
                len(summaries_data) + len(tags_data) + len(collections_data) + len(highlights_data)
            )
            created_at = datetime.now(UTC).isoformat()
            manifest = {
                "version": BACKUP_SCHEMA_VERSION,
                "schema_version": BACKUP_SCHEMA_VERSION,
                "user_id": user_id,
                "created_at": created_at,
                "counts": {
                    "requests": len(requests_data),
                    "summaries": len(summaries_data),
                    "tags": len(tags_data),
                    "summary_tags": len(summary_tags_data),
                    "collections": len(collections_data),
                    "collection_items": len(collection_items_data),
                    "highlights": len(highlights_data),
                },
            }

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

        def _build_and_write_archive() -> tuple[Path, int, dict[str, Any]]:
            # Zip serialization, Fernet encryption, and the file write are all
            # CPU/IO-bound and must not run on the event loop; offload via to_thread.
            os.makedirs(backup_dir, exist_ok=True)
            buf = BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, default=str, indent=2))
                archive.writestr("requests.json", json.dumps(requests_data, default=str))
                archive.writestr("summaries.json", json.dumps(summaries_data, default=str))
                archive.writestr("tags.json", json.dumps(tags_data, default=str))
                archive.writestr("summary_tags.json", json.dumps(summary_tags_data, default=str))
                archive.writestr("collections.json", json.dumps(collections_data, default=str))
                archive.writestr(
                    "collection_items.json", json.dumps(collection_items_data, default=str)
                )
                archive.writestr("highlights.json", json.dumps(highlights_data, default=str))
                archive.writestr(
                    "preferences.json",
                    json.dumps(preferences, default=str) if preferences else "{}",
                )

            zip_bytes = buf.getvalue()
            if cfg.is_encryption_enabled:
                payload = encrypt_backup(zip_bytes, cfg.encryption_key)
                suffix = ".zip.enc"
            else:
                payload = zip_bytes
                suffix = ".zip"

            archive_verification = verify_backup_archive(payload, cfg=cfg)
            archive_path = backup_dir / f"ratatoskr-backup-{user_id}-{timestamp}{suffix}"
            archive_path.write_bytes(payload)
            return archive_path, len(payload), archive_verification

        zip_path, file_size, verification = await asyncio.to_thread(_build_and_write_archive)
        async with database.transaction() as session:
            await session.execute(
                update(UserBackup)
                .where(UserBackup.id == backup_id)
                .values(
                    file_path=str(zip_path),
                    file_size_bytes=file_size,
                    items_count=items_count,
                    checksum_sha256=verification["checksum"],
                    item_counts_json=verification["item_counts"],
                    schema_version=verification["schema_version"],
                    verified_at=datetime.fromisoformat(verification["verified_at"]),
                    verification_status=verification["verification_status"],
                    verification_error=verification["verification_error"],
                    status="completed",
                    updated_at=_utcnow(),
                )
            )
        retention_count = _coerce_retention_count(preferences)
        if retention_count is not None:
            cleanup = await async_cleanup_old_user_backups(
                database,
                user_id=user_id,
                keep_count=retention_count,
            )
            if cleanup["deleted"]:
                logger.info(
                    "backup_retention_cleanup_completed",
                    extra={"user_id": user_id, "backup_id": backup_id, **cleanup},
                )
        logger.info(
            "backup_created",
            extra={
                "backup_id": backup_id,
                "user_id": user_id,
                "file_size": file_size,
                "items_count": items_count,
            },
        )
    except Exception as exc:
        logger.exception(
            "backup_creation_failed",
            extra={"backup_id": backup_id, "user_id": user_id, "error": str(exc)},
        )
        async with database.transaction() as session:
            await session.execute(
                update(UserBackup)
                .where(UserBackup.id == backup_id)
                .values(status="failed", error=str(exc)[:1000], updated_at=_utcnow())
            )


async def async_restore_from_archive(
    user_id: int,
    zip_bytes: bytes,
    *,
    db: Database | None = None,
    cfg: BackupConfig | None = None,
) -> dict[str, Any]:
    """Restore user data from a backup ZIP and return a summary."""
    from app.config.backup import load_backup_config

    restored: dict[str, int] = {
        "requests": 0,
        "summaries": 0,
        "tags": 0,
        "summary_tags": 0,
        "collections": 0,
        "collection_items": 0,
        "highlights": 0,
    }
    skipped: dict[str, int] = {
        "requests": 0,
        "summaries": 0,
        "tags": 0,
        "collections": 0,
    }
    errors: list[str] = []

    cfg = cfg or load_backup_config()

    if is_fernet_ciphertext(zip_bytes):
        if cfg.encryption_key is None:
            errors.append("Encrypted backup but BACKUP_ENCRYPTION_KEY is not configured")
            return {"restored": restored, "skipped": skipped, "errors": errors}
        try:
            zip_bytes = decrypt_backup(zip_bytes, cfg.encryption_key)
        except InvalidBackupCiphertextError:
            errors.append("Could not decrypt backup (wrong key or corrupted archive)")
            return {"restored": restored, "skipped": skipped, "errors": errors}
    else:
        logger.warning("restore_unencrypted_backup", extra={"user_id": user_id})

    try:
        validate_zip_safety(
            zip_bytes,
            max_entries=cfg.max_zip_entries,
            max_compressed_bytes=cfg.max_compressed_bytes,
            max_decompressed_bytes=cfg.max_decompressed_bytes,
            max_ratio=cfg.max_compression_ratio,
        )
    except ZipSafetyViolation as exc:
        errors.append(str(exc))
        return {"restored": restored, "skipped": skipped, "errors": errors}

    try:
        with zipfile.ZipFile(BytesIO(zip_bytes), "r") as archive:
            manifest = _read_json(archive, "manifest.json")
            archive_version = manifest.get("version", "unknown")
            if archive_version not in ("1.0",):
                return {
                    "restored": restored,
                    "skipped": skipped,
                    "errors": [f"Unsupported backup version: {archive_version}"],
                }

            requests_data = _read_json(archive, "requests.json")
            summaries_data = _read_json(archive, "summaries.json")
            tags_data = _read_json(archive, "tags.json")
            summary_tags_data = _read_json(archive, "summary_tags.json")
            collections_data = _read_json(archive, "collections.json")
            collection_items_data = _read_json(archive, "collection_items.json")
            highlights_data = _read_json(archive, "highlights.json")

        database = _database(db)
        async with database.transaction() as session:
            request_id_map: dict[int, int] = {}
            summary_id_map: dict[int, int] = {}
            tag_id_map: dict[int, int] = {}
            collection_id_map: dict[int, int] = {}

            for request in requests_data:
                try:
                    dedupe = request.get("dedupe_hash")
                    old_request_id = int(request["id"])
                    if dedupe:
                        existing = await session.scalar(
                            select(Request).where(
                                Request.user_id == user_id,
                                Request.dedupe_hash == dedupe,
                            )
                        )
                        if existing:
                            request_id_map[old_request_id] = existing.id
                            skipped["requests"] += 1
                            continue

                    new_request = Request(
                        type=request.get("type", "url"),
                        status=request.get("status", "completed"),
                        user_id=user_id,
                        input_url=request.get("input_url"),
                        normalized_url=request.get("normalized_url"),
                        dedupe_hash=request.get("dedupe_hash"),
                        lang_detected=request.get("lang_detected"),
                    )
                    session.add(new_request)
                    await session.flush()
                    request_id_map[old_request_id] = new_request.id
                    restored["requests"] += 1
                except Exception as exc:
                    errors.append(f"request {request.get('id')}: {exc}")

            for summary in summaries_data:
                try:
                    old_request_id = _old_id(summary, "request_id", "request")
                    new_request_id = request_id_map.get(old_request_id or -1)
                    if new_request_id is None:
                        skipped["summaries"] += 1
                        continue

                    existing_summary = await session.scalar(
                        select(Summary).where(Summary.request_id == new_request_id)
                    )
                    old_summary_id = int(summary["id"])
                    if existing_summary:
                        summary_id_map[old_summary_id] = existing_summary.id
                        skipped["summaries"] += 1
                        continue

                    new_summary = Summary(
                        request_id=new_request_id,
                        lang=summary.get("lang", "en"),
                        json_payload=summary.get("json_payload"),
                        is_read=bool(summary.get("is_read", False)),
                        is_deleted=bool(summary.get("is_deleted", False)),
                    )
                    session.add(new_summary)
                    await session.flush()
                    summary_id_map[old_summary_id] = new_summary.id
                    restored["summaries"] += 1
                except Exception as exc:
                    errors.append(f"summary {summary.get('id')}: {exc}")

            for tag in tags_data:
                try:
                    normalized_name = (
                        tag.get("normalized_name") or tag.get("name", "").strip().lower()
                    )
                    existing_tag = await session.scalar(
                        select(Tag).where(
                            Tag.user_id == user_id,
                            Tag.normalized_name == normalized_name,
                            Tag.is_deleted.is_(False),
                        )
                    )
                    old_tag_id = int(tag["id"])
                    if existing_tag:
                        tag_id_map[old_tag_id] = existing_tag.id
                        skipped["tags"] += 1
                        continue
                    new_tag = Tag(
                        user_id=user_id,
                        name=tag.get("name", normalized_name),
                        normalized_name=normalized_name,
                        color=tag.get("color"),
                    )
                    session.add(new_tag)
                    await session.flush()
                    tag_id_map[old_tag_id] = new_tag.id
                    restored["tags"] += 1
                except Exception as exc:
                    errors.append(f"tag {tag.get('id')}: {exc}")

            for summary_tag in summary_tags_data:
                try:
                    new_summary_id = summary_id_map.get(
                        _old_id(summary_tag, "summary_id", "summary") or -1
                    )
                    new_tag_id = tag_id_map.get(_old_id(summary_tag, "tag_id", "tag") or -1)
                    if new_summary_id is None or new_tag_id is None:
                        continue
                    existing = await session.scalar(
                        select(SummaryTag).where(
                            SummaryTag.summary_id == new_summary_id,
                            SummaryTag.tag_id == new_tag_id,
                        )
                    )
                    if existing is not None:
                        continue
                    session.add(
                        SummaryTag(
                            summary_id=new_summary_id,
                            tag_id=new_tag_id,
                            source=summary_tag.get("source", "manual"),
                        )
                    )
                    await session.flush()
                    restored["summary_tags"] += 1
                except Exception as exc:
                    errors.append(f"summary_tag {summary_tag.get('id')}: {exc}")

            for collection in collections_data:
                try:
                    existing_collection = await session.scalar(
                        select(Collection).where(
                            Collection.user_id == user_id,
                            Collection.name == collection.get("name"),
                            Collection.is_deleted.is_(False),
                        )
                    )
                    old_collection_id = int(collection["id"])
                    if existing_collection:
                        collection_id_map[old_collection_id] = existing_collection.id
                        skipped["collections"] += 1
                        continue
                    new_collection = Collection(
                        user_id=user_id,
                        name=collection.get("name", "Imported collection"),
                        description=collection.get("description"),
                        position=collection.get("position"),
                        collection_type=collection.get("collection_type", "manual"),
                        query_conditions_json=collection.get("query_conditions_json"),
                        query_match_mode=collection.get("query_match_mode", "all"),
                    )
                    session.add(new_collection)
                    await session.flush()
                    collection_id_map[old_collection_id] = new_collection.id
                    restored["collections"] += 1
                except Exception as exc:
                    errors.append(f"collection {collection.get('id')}: {exc}")

            for item in collection_items_data:
                try:
                    new_collection_id = collection_id_map.get(
                        _old_id(item, "collection_id", "collection") or -1
                    )
                    new_summary_id = summary_id_map.get(
                        _old_id(item, "summary_id", "summary") or -1
                    )
                    if new_collection_id is None or new_summary_id is None:
                        continue
                    existing = await session.scalar(
                        select(CollectionItem).where(
                            CollectionItem.collection_id == new_collection_id,
                            CollectionItem.summary_id == new_summary_id,
                        )
                    )
                    if existing is not None:
                        continue
                    session.add(
                        CollectionItem(
                            collection_id=new_collection_id,
                            summary_id=new_summary_id,
                            position=item.get("position"),
                        )
                    )
                    await session.flush()
                    restored["collection_items"] += 1
                except Exception as exc:
                    errors.append(f"collection_item {item.get('id')}: {exc}")

            for highlight in highlights_data:
                try:
                    new_summary_id = summary_id_map.get(
                        _old_id(highlight, "summary_id", "summary") or -1
                    )
                    if new_summary_id is None:
                        continue
                    session.add(
                        SummaryHighlight(
                            user_id=user_id,
                            summary_id=new_summary_id,
                            text=highlight.get("text", ""),
                            start_offset=highlight.get("start_offset"),
                            end_offset=highlight.get("end_offset"),
                            color=highlight.get("color"),
                            note=highlight.get("note"),
                        )
                    )
                    restored["highlights"] += 1
                except Exception as exc:
                    errors.append(f"highlight {highlight.get('id')}: {exc}")
    except KeyError as exc:
        errors.append(f"Missing required file in backup archive: {exc}")
    except zipfile.BadZipFile:
        errors.append("Invalid or corrupt ZIP archive")
    except Exception as exc:
        errors.append(str(exc))

    return {"restored": restored, "skipped": skipped, "errors": errors}


async def async_dry_run_restore_from_archive(
    user_id: int,
    zip_bytes: bytes,
    *,
    db: Database | None = None,
    cfg: BackupConfig | None = None,
) -> dict[str, Any]:
    """Validate a backup and estimate restore effects without mutating the database."""
    from app.config.backup import load_backup_config

    cfg = cfg or load_backup_config()
    inspection, errors = inspect_backup_archive(zip_bytes, cfg=cfg)
    counts = inspection.counts if inspection else _empty_restore_counts()
    result: dict[str, Any] = {
        "valid": not errors,
        "compatible": bool(inspection and inspection.schema_version == BACKUP_SCHEMA_VERSION),
        "schema_version": inspection.schema_version if inspection else None,
        "backup_created_at": inspection.created_at if inspection else None,
        "encrypted": bool(inspection.encrypted) if inspection else is_fernet_ciphertext(zip_bytes),
        "counts": counts,
        "estimated_affected_rows": counts,
        "estimated_skipped_rows": {
            "requests": 0,
            "tags": 0,
            "collections": 0,
        },
        "errors": errors,
    }
    if errors or inspection is None:
        return result

    try:
        payload, _encrypted = _decrypt_archive_payload(zip_bytes, cfg, errors=errors)
        if payload is None:
            result["valid"] = False
            result["errors"] = errors
            return result
        with zipfile.ZipFile(BytesIO(payload), "r") as archive:
            requests_data = _read_json(archive, "requests.json")
            tags_data = _read_json(archive, "tags.json")
            collections_data = _read_json(archive, "collections.json")
    except Exception as exc:
        errors.append(str(exc))
        result["valid"] = False
        result["errors"] = errors
        return result

    if db is None:
        return result

    database = _database(db)
    skipped = cast("dict[str, int]", result["estimated_skipped_rows"])
    async with database.session() as session:
        for request in requests_data:
            dedupe = request.get("dedupe_hash") if isinstance(request, dict) else None
            if not dedupe:
                continue
            existing = await session.scalar(
                select(Request).where(Request.user_id == user_id, Request.dedupe_hash == dedupe)
            )
            if existing:
                skipped["requests"] += 1

        for tag in tags_data:
            if not isinstance(tag, dict):
                continue
            normalized_name = tag.get("normalized_name") or tag.get("name", "").strip().lower()
            if not normalized_name:
                continue
            existing = await session.scalar(
                select(Tag).where(
                    Tag.user_id == user_id,
                    Tag.normalized_name == normalized_name,
                    Tag.is_deleted.is_(False),
                )
            )
            if existing:
                skipped["tags"] += 1

        for collection in collections_data:
            name = collection.get("name") if isinstance(collection, dict) else None
            if not name:
                continue
            existing = await session.scalar(
                select(Collection).where(
                    Collection.user_id == user_id,
                    Collection.name == name,
                    Collection.is_deleted.is_(False),
                )
            )
            if existing:
                skipped["collections"] += 1

    result["estimated_skipped_rows"] = skipped
    result["estimated_affected_rows"] = {
        key: max(0, int(value) - skipped.get(key, 0)) for key, value in counts.items()
    }
    result["errors"] = errors
    result["valid"] = not errors
    return result


def create_backup_archive(
    user_id: int,
    backup_id: int,
    *,
    db: Database | None = None,
    data_dir: str | None = None,
    cfg: BackupConfig | None = None,
) -> None:
    """Synchronous compatibility wrapper for backup archive creation."""
    asyncio.run(
        async_create_backup_archive(
            user_id=user_id,
            backup_id=backup_id,
            db=db,
            data_dir=data_dir,
            cfg=cfg,
        )
    )


def restore_from_archive(
    user_id: int,
    zip_bytes: bytes,
    *,
    db: Database | None = None,
    cfg: BackupConfig | None = None,
) -> dict[str, Any]:
    """Synchronous compatibility wrapper for backup archive restore."""
    return asyncio.run(
        async_restore_from_archive(user_id=user_id, zip_bytes=zip_bytes, db=db, cfg=cfg)
    )


def dry_run_restore_from_archive(
    user_id: int,
    zip_bytes: bytes,
    *,
    db: Database | None = None,
    cfg: BackupConfig | None = None,
) -> dict[str, Any]:
    """Synchronous compatibility wrapper for non-mutating archive restore dry-runs."""
    return asyncio.run(
        async_dry_run_restore_from_archive(user_id=user_id, zip_bytes=zip_bytes, db=db, cfg=cfg)
    )
