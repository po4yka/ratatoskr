"""Import upload size and item-count limits configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ImportConfig(BaseModel):
    """Upload size and item-count limits for the import endpoint."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    max_upload_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1024,
        validation_alias="IMPORT_MAX_UPLOAD_BYTES",
        description="Maximum upload size in bytes for the import endpoint (default 10 MB).",
    )
    max_items: int = Field(
        default=10_000,
        ge=1,
        validation_alias="IMPORT_MAX_ITEMS",
        description="Maximum number of parsed bookmarks per import (default 10 000).",
    )
