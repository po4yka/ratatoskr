"""Backup archive restore (full write and dry-run read-only).

Public surface (re-exported by backup_archive_service):
    async_restore_from_archive
    async_dry_run_restore_from_archive
    restore_from_archive
    dry_run_restore_from_archive

Inspection and verification live in backup_inspector.py.
Shared constants and pure helpers live in backup_writer.py.
"""

from __future__ import annotations

import asyncio
import zipfile
from io import BytesIO
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from app.db.models import (
    Collection,
    CollectionItem,
    Request,
    Summary,
    SummaryHighlight,
    SummaryTag,
    Tag,
)
from app.infrastructure.persistence.backup_crypto import (
    is_fernet_ciphertext,
    is_streaming_ciphertext,
)
from app.infrastructure.persistence.backup_inspector import (
    _decrypt_archive_payload,
    _empty_restore_counts,
    inspect_backup_archive,
)
from app.infrastructure.persistence.backup_writer import (
    BACKUP_SCHEMA_VERSION,
    _database,
    _read_json,
)

if TYPE_CHECKING:
    from app.config.backup import BackupConfig
    from app.db.session import Database

# ---------------------------------------------------------------------------
# Private helper
# ---------------------------------------------------------------------------


def _old_id(row: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return int(value)
    return None


class _RestoreAbort(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("backup restore aborted")
        self.errors = errors


# ---------------------------------------------------------------------------
# Full restore
# ---------------------------------------------------------------------------


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

    inspection, errors = inspect_backup_archive(zip_bytes, cfg=cfg)
    if errors or inspection is None:
        return {"restored": restored, "skipped": skipped, "errors": errors}

    zip_bytes, _encrypted = _decrypt_archive_payload(zip_bytes, cfg, errors=errors)
    if zip_bytes is None:
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

            if errors:
                raise _RestoreAbort(errors)
    except _RestoreAbort as exc:
        errors = exc.errors
        restored = dict.fromkeys(restored, 0)
    except KeyError as exc:
        errors.append(f"Missing required file in backup archive: {exc}")
    except zipfile.BadZipFile:
        errors.append("Invalid or corrupt ZIP archive")
    except Exception as exc:
        errors.append(str(exc))
        restored = dict.fromkeys(restored, 0)

    return {"restored": restored, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Dry-run restore
# ---------------------------------------------------------------------------


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
        "encrypted": bool(inspection.encrypted)
        if inspection
        else (is_fernet_ciphertext(zip_bytes) or is_streaming_ciphertext(zip_bytes)),
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


# ---------------------------------------------------------------------------
# Synchronous compatibility wrappers
# ---------------------------------------------------------------------------


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
