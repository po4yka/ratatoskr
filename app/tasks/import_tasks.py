"""Taskiq task: durable bookmark import processing."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from taskiq import TaskiqDepends

from app.application.dto.import_bookmarks import ImportBookmarksCommand
from app.application.use_cases.import_pipeline import ImportBookmarksUseCase
from app.core.logging_utils import get_logger
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.domain.services.import_parsers.base import ImportedBookmark
from app.infrastructure.persistence.repositories.bookmark_import_repository import (
    BookmarkImportAdapter,
)
from app.infrastructure.persistence.repositories.import_job_repository import (
    ImportJobRepositoryAdapter,
)
from app.tasks.broker import broker
from app.tasks.deps import get_db

logger = get_logger(__name__)


@broker.task(task_name="ratatoskr.import.process_bookmarks", retry_on_error=True, max_retries=3)
async def process_import_job(
    job_id: int,
    user_id: int,
    bookmarks_json: list[dict[str, Any]],
    options: dict[str, Any],
    db: Database = TaskiqDepends(get_db),
) -> None:
    """Process a bookmark import job via Taskiq worker."""
    await _run_import_body(
        job_id=job_id,
        user_id=user_id,
        bookmarks_json=bookmarks_json,
        options=options,
        db=db,
    )


async def _run_import_body(
    *,
    job_id: int,
    user_id: int,
    bookmarks_json: list[dict[str, Any]],
    options: dict[str, Any],
    db: Database,
) -> None:
    """Core import logic — separated for testability."""
    import_job_repo = ImportJobRepositoryAdapter(db)

    job = await import_job_repo.async_get_job(job_id)
    if job is None:
        logger.warning("import_job_not_found", extra={"job_id": job_id})
        return
    if job["status"] != "pending":
        logger.info(
            "import_job_skipped_not_pending",
            extra={"job_id": job_id, "status": job["status"]},
        )
        return

    bookmark_import_repo = BookmarkImportAdapter(db)
    bookmarks = [_dict_to_bookmark(d) for d in bookmarks_json]

    use_case = ImportBookmarksUseCase(
        import_job_repository=import_job_repo,
        bookmark_import_repository=bookmark_import_repo,
    )
    await use_case.execute(
        ImportBookmarksCommand(
            job_id=job_id,
            bookmarks=bookmarks,
            user_id=user_id,
            options=options,
        )
    )


def _dict_to_bookmark(d: dict[str, Any]) -> ImportedBookmark:
    """Reconstruct an ImportedBookmark from a JSON-serialized dict."""
    raw_ts = d.get("created_at")
    return ImportedBookmark(
        url=d["url"],
        title=d.get("title"),
        tags=d.get("tags") or [],
        notes=d.get("notes"),
        created_at=datetime.fromisoformat(raw_ts) if raw_ts else None,
        collection_name=d.get("collection_name"),
        highlights=d.get("highlights"),
        extra=d.get("extra") or {},
    )
