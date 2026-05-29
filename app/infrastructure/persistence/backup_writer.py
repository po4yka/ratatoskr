"""Backup archive creation, retention pruning, and shared constants/helpers.

Public surface (re-exported by backup_archive_service):
    BACKUP_SCHEMA_VERSION
    BackupArchiveInspection  -- imported from backup_reader; re-exported here for callers
    calculate_backup_checksum
    async_cleanup_old_user_backups
    async_create_backup_archive
    create_backup_archive
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
from app.db.runtime_database import resolve_runtime_database
from app.db.types import _utcnow
from app.infrastructure.persistence.backup_crypto import encrypt_backup

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


# ---------------------------------------------------------------------------
# Shared pure helpers (used by both writer and reader)
# ---------------------------------------------------------------------------


def _database(db: Database | None) -> Database:
    if db is not None:
        return db
    return resolve_runtime_database()


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


# ---------------------------------------------------------------------------
# Retention pruning
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Archive creation
# ---------------------------------------------------------------------------


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
    from app.infrastructure.persistence.backup_inspector import verify_backup_archive

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


# ---------------------------------------------------------------------------
# Synchronous compatibility wrapper
# ---------------------------------------------------------------------------


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
