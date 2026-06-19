"""Bookmark import orchestration use case."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.application.dto.import_bookmarks import (
    ImportBookmarksCommand,
    ImportProgressSnapshot,
)
from app.application.use_cases._tracing import use_case_span
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.application.ports.imports import BookmarkImportPort, ImportJobRepositoryPort

logger = get_logger(__name__)


class ImportBookmarksUseCase:
    """Run a bookmark import job through dedicated application ports."""

    def __init__(
        self,
        *,
        import_job_repository: ImportJobRepositoryPort,
        bookmark_import_repository: BookmarkImportPort,
        progress_flush_interval: int = 10,
    ) -> None:
        self._import_job_repo = import_job_repository
        self._bookmark_import_repo = bookmark_import_repository
        self._flush_interval = max(1, progress_flush_interval)

    async def execute(self, command: ImportBookmarksCommand) -> ImportProgressSnapshot:
        """Process the uploaded bookmarks and keep the ImportJob row in sync."""
        with use_case_span(
            "import_bookmarks.execute",
            command,
            attributes={"ratatoskr.import.job_id": command.job_id},
        ):
            processed = 0
            created = 0
            skipped = 0
            failed = 0
            errors: list[str] = []

            try:
                await self._import_job_repo.async_set_status(command.job_id, "processing")
                for index, bookmark in enumerate(command.bookmarks, start=1):
                    try:
                        result = await self._bookmark_import_repo.async_import_bookmark(
                            bookmark,
                            user_id=command.user_id,
                            options=command.options,
                        )
                        if result.outcome == "created":
                            created += 1
                        elif result.outcome == "skipped":
                            skipped += 1
                        else:
                            failed += 1
                            errors.append(result.error or f"{result.url}: import failed")
                    except Exception as exc:
                        failed += 1
                        errors.append(f"{bookmark.url}: {exc}")
                        logger.warning(
                            "import_bookmark_failed",
                            extra={
                                "job_id": command.job_id,
                                "url": bookmark.url[:200],
                                "error": str(exc),
                            },
                        )
                    finally:
                        processed = index

                    if processed % self._flush_interval == 0:
                        await self._flush_progress(
                            command.job_id,
                            ImportProgressSnapshot(
                                processed=processed,
                                created=created,
                                skipped=skipped,
                                failed=failed,
                                errors=list(errors),
                            ),
                        )

                final_status = "failed" if created == 0 and failed > 0 else "completed"
                snapshot = ImportProgressSnapshot(
                    processed=processed,
                    created=created,
                    skipped=skipped,
                    failed=failed,
                    errors=list(errors),
                    status=final_status,
                )
                await self._flush_progress(command.job_id, snapshot)
                await self._import_job_repo.async_set_status(command.job_id, final_status)
                logger.info(
                    "import_job_finished",
                    extra={
                        "job_id": command.job_id,
                        "status": final_status,
                        "created": created,
                        "skipped": skipped,
                        "failed": failed,
                    },
                )
                return snapshot
            except Exception as exc:
                logger.exception("import_job_crashed", extra={"job_id": command.job_id})
                snapshot = ImportProgressSnapshot(
                    processed=processed,
                    created=created,
                    skipped=skipped,
                    failed=failed + 1,
                    errors=[*errors, str(exc)],
                    status="failed",
                )
                await self._flush_progress(command.job_id, snapshot)
                await self._import_job_repo.async_set_status(command.job_id, "failed")
                return snapshot

    async def _flush_progress(self, job_id: int, snapshot: ImportProgressSnapshot) -> None:
        await self._import_job_repo.async_update_progress(
            job_id,
            processed=snapshot.processed,
            created=snapshot.created,
            skipped=snapshot.skipped,
            failed=snapshot.failed,
            errors=snapshot.errors,
        )
