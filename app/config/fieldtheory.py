"""Fieldtheory-cli integration configuration.

Drives the Taskiq bookmark delta-scan that ingests rows from the host-side
fieldtheory-cli (`ft`) SQLite database into Postgres. See
`docs/explanation/fieldtheory-integration.md` for the full integration design.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FieldTheoryConfig(BaseModel):
    """Fieldtheory bookmark-ingestor configuration.

    The host runs `ft sync` on its own schedule (typically hourly); inside the
    container, this Taskiq job periodically delta-scans the read-only SQLite
    bookmarks database and ingests new rows into the Ratatoskr `requests` +
    `fieldtheory_bookmark_metadata` pair.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=True,
        validation_alias="FIELDTHEORY_SYNC_ENABLED",
        description=(
            "Enable the periodic fieldtheory bookmark delta-scan Taskiq job; "
            "when false, the job is not registered with the scheduler."
        ),
    )
    sync_cron: str = Field(
        default="*/15 * * * *",
        validation_alias="FIELDTHEORY_SYNC_CRON",
        description="UTC cron expression for the bookmark delta-scan job.",
    )
    bookmarks_db_path: str = Field(
        default="/fieldtheory/bookmarks.db",
        validation_alias="FIELDTHEORY_BOOKMARKS_DB_PATH",
        description=(
            "Path to the read-only `ft` SQLite bookmarks database inside the "
            "container; typically the mount target of `~/.fieldtheory/bookmarks.db`."
        ),
    )

    @field_validator("sync_cron", mode="before")
    @classmethod
    def _validate_sync_cron(cls, value: Any) -> str:
        if value in (None, ""):
            return "*/15 * * * *"
        cron = str(value).strip()
        if len(cron.split()) != 5:
            msg = "Fieldtheory sync cron must be a 5-field cron expression"
            raise ValueError(msg)
        return cron

    @field_validator("bookmarks_db_path", mode="before")
    @classmethod
    def _validate_bookmarks_db_path(cls, value: Any) -> str:
        if value in (None, ""):
            return "/fieldtheory/bookmarks.db"
        path = str(value).strip()
        if not path:
            msg = "Fieldtheory bookmarks_db_path must be a non-empty path"
            raise ValueError(msg)
        return path
