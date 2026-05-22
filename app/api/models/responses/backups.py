"""Backup and import API response models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BackupResponse(BaseModel):
    id: int
    type: str
    status: str
    file_path: str | None = Field(default=None, serialization_alias="filePath")
    file_size_bytes: int | None = Field(default=None, serialization_alias="fileSizeBytes")
    items_count: int | None = Field(default=None, serialization_alias="itemsCount")
    checksum_sha256: str | None = Field(default=None, serialization_alias="checksumSha256")
    item_counts: dict[str, int] = Field(default_factory=dict, serialization_alias="itemCounts")
    schema_version: str | None = Field(default=None, serialization_alias="schemaVersion")
    verified_at: str | None = Field(default=None, serialization_alias="verifiedAt")
    verification_status: str | None = Field(default=None, serialization_alias="verificationStatus")
    verification_error: str | None = Field(default=None, serialization_alias="verificationError")
    error: str | None = None
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")


class RestoreDryRunResponse(BaseModel):
    valid: bool
    compatible: bool
    schema_version: str | None = Field(default=None, serialization_alias="schemaVersion")
    backup_created_at: str | None = Field(default=None, serialization_alias="backupCreatedAt")
    encrypted: bool
    counts: dict[str, int] = Field(default_factory=dict)
    estimated_affected_rows: dict[str, int] = Field(
        default_factory=dict,
        serialization_alias="estimatedAffectedRows",
    )
    estimated_skipped_rows: dict[str, int] = Field(
        default_factory=dict,
        serialization_alias="estimatedSkippedRows",
    )
    errors: list[str] = Field(default_factory=list)


class ImportJobResponse(BaseModel):
    id: int
    source_format: str = Field(serialization_alias="sourceFormat")
    file_name: str | None = Field(default=None, serialization_alias="fileName")
    status: str
    total_items: int = Field(serialization_alias="totalItems")
    processed_items: int = Field(serialization_alias="processedItems")
    created_items: int = Field(serialization_alias="createdItems")
    skipped_items: int = Field(serialization_alias="skippedItems")
    failed_items: int = Field(serialization_alias="failedItems")
    errors: list[str] = Field(default_factory=list)
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")
