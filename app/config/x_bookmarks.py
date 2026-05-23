"""X-cli integration configuration.

Drives the Taskiq bookmark delta-scan that ingests rows from the host-side
fieldtheory-cli (`ft`) SQLite database into Postgres. See
`docs/explanation/x-bookmarks-integration.md` for the full integration design.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class XBookmarksConfig(BaseModel):
    """X bookmark- and wiki-ingestor configuration.

    The host runs `ft sync` on its own schedule (typically hourly); inside the
    container, two Taskiq jobs periodically delta-scan the host-mounted
    x_bookmarks data: the bookmark job reads the read-only SQLite bookmarks
    database into the Ratatoskr `requests` + `x_bookmark_metadata`
    pair, and the wiki job re-embeds changed pages from the on-disk wiki
    library directly into the shared Qdrant collection as
    `entity_type="x_wiki"` points -- the wiki has no Postgres
    table; Qdrant is its sole persistence beyond the source filesystem. The
    `enabled` flag is the master switch for BOTH the bookmark and the wiki
    sync jobs.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=True,
        validation_alias="X_BOOKMARKS_SYNC_ENABLED",
        description=(
            "Master switch for the periodic x_bookmarks delta-scan Taskiq jobs "
            "(covers BOTH the bookmark and the wiki sync); when false, neither "
            "job is registered with the scheduler."
        ),
    )
    sync_cron: str = Field(
        default="*/15 * * * *",
        validation_alias="X_BOOKMARKS_SYNC_CRON",
        description="UTC cron expression for the bookmark delta-scan job.",
    )
    bookmarks_db_path: str = Field(
        default="/x_bookmarks/bookmarks.db",
        validation_alias="X_BOOKMARKS_DB_PATH",
        description=(
            "Path to the read-only `ft` SQLite bookmarks database inside the "
            "container; typically the mount target of `~/.x_bookmarks/bookmarks.db`."
        ),
    )
    wiki_sync_cron: str = Field(
        default="0 * * * *",
        validation_alias="X_WIKI_SYNC_CRON",
        description="UTC cron expression for the wiki library delta-scan job.",
    )
    library_path: str = Field(
        default="/x_bookmarks/library",
        validation_alias="X_WIKI_LIBRARY_PATH",
        description=(
            "Path to the read-only `ft` wiki library directory inside the "
            "container; typically the mount target of `~/.x_bookmarks/library`."
        ),
    )
    ideas_path: str = Field(
        default="/x_bookmarks/ideas",
        validation_alias="X_IDEAS_PATH",
        description=(
            "Path to the read-only `ft` Possible-run output directory inside the "
            "container; typically the mount target of `~/.x_bookmarks/ideas`. "
            "Consumed by the `/x_possible` Telegram handler, which "
            "reads the newest `*.json` file on user gesture."
        ),
    )

    @field_validator("sync_cron", mode="before")
    @classmethod
    def _validate_sync_cron(cls, value: Any) -> str:
        if value in (None, ""):
            return "*/15 * * * *"
        cron = str(value).strip()
        if len(cron.split()) != 5:
            msg = "X sync cron must be a 5-field cron expression"
            raise ValueError(msg)
        return cron

    @field_validator("bookmarks_db_path", mode="before")
    @classmethod
    def _validate_bookmarks_db_path(cls, value: Any) -> str:
        if value in (None, ""):
            return "/x_bookmarks/bookmarks.db"
        path = str(value).strip()
        if not path:
            msg = "X bookmarks_db_path must be a non-empty path"
            raise ValueError(msg)
        return path

    @field_validator("wiki_sync_cron", mode="before")
    @classmethod
    def _validate_wiki_sync_cron(cls, value: Any) -> str:
        if value in (None, ""):
            return "0 * * * *"
        cron = str(value).strip()
        if len(cron.split()) != 5:
            msg = "X wiki sync cron must be a 5-field cron expression"
            raise ValueError(msg)
        return cron

    @field_validator("library_path", mode="before")
    @classmethod
    def _validate_library_path(cls, value: Any) -> str:
        if value in (None, ""):
            return "/x_bookmarks/library"
        path = str(value).strip()
        if not path:
            msg = "X library_path must be a non-empty path"
            raise ValueError(msg)
        return path

    @field_validator("ideas_path", mode="before")
    @classmethod
    def _validate_ideas_path(cls, value: Any) -> str:
        if value in (None, ""):
            return "/x_bookmarks/ideas"
        path = str(value).strip()
        if not path:
            msg = "X ideas_path must be a non-empty path"
            raise ValueError(msg)
        return path
