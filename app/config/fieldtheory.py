"""Fieldtheory-cli integration configuration.

Drives the Taskiq bookmark delta-scan that ingests rows from the host-side
fieldtheory-cli (`ft`) SQLite database into Postgres. See
`docs/explanation/fieldtheory-integration.md` for the full integration design.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FieldTheoryConfig(BaseModel):
    """Fieldtheory bookmark- and wiki-ingestor configuration.

    The host runs `ft sync` on its own schedule (typically hourly); inside the
    container, two Taskiq jobs periodically delta-scan the host-mounted
    fieldtheory data: the bookmark job reads the read-only SQLite bookmarks
    database into the Ratatoskr `requests` + `fieldtheory_bookmark_metadata`
    pair, and the wiki job re-embeds changed pages from the on-disk wiki
    library directly into the shared Qdrant collection as
    `entity_type="fieldtheory_wiki"` points -- the wiki has no Postgres
    table; Qdrant is its sole persistence beyond the source filesystem. The
    `enabled` flag is the master switch for BOTH the bookmark and the wiki
    sync jobs.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=True,
        validation_alias="FIELDTHEORY_SYNC_ENABLED",
        description=(
            "Master switch for the periodic fieldtheory delta-scan Taskiq jobs "
            "(covers BOTH the bookmark and the wiki sync); when false, neither "
            "job is registered with the scheduler."
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
    wiki_sync_cron: str = Field(
        default="0 * * * *",
        validation_alias="FIELDTHEORY_WIKI_SYNC_CRON",
        description="UTC cron expression for the wiki library delta-scan job.",
    )
    library_path: str = Field(
        default="/fieldtheory/library",
        validation_alias="FIELDTHEORY_LIBRARY_PATH",
        description=(
            "Path to the read-only `ft` wiki library directory inside the "
            "container; typically the mount target of `~/.fieldtheory/library`."
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

    @field_validator("wiki_sync_cron", mode="before")
    @classmethod
    def _validate_wiki_sync_cron(cls, value: Any) -> str:
        if value in (None, ""):
            return "0 * * * *"
        cron = str(value).strip()
        if len(cron.split()) != 5:
            msg = "Fieldtheory wiki sync cron must be a 5-field cron expression"
            raise ValueError(msg)
        return cron

    @field_validator("library_path", mode="before")
    @classmethod
    def _validate_library_path(cls, value: Any) -> str:
        if value in (None, ""):
            return "/fieldtheory/library"
        path = str(value).strip()
        if not path:
            msg = "Fieldtheory library_path must be a non-empty path"
            raise ValueError(msg)
        return path
