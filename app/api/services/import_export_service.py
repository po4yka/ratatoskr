"""Service logic for import/export API endpoints."""

from __future__ import annotations

from typing import Any, cast

from app.api.dependencies.database import (
    get_bookmark_import_repository,
    get_import_job_repository,
    get_session_manager,
)
from app.api.exceptions import ResourceNotFoundError
from app.api.models.responses import ImportJobResponse
from app.api.search_helpers import isotime
from app.db.session import Database  # noqa: TC001  # used at runtime in __init__ signature
from app.infrastructure.persistence.repositories.user_content_repository import (
    UserContentRepositoryAdapter,
)


class ImportExportService:
    """Owns import job tracking and export dataset assembly."""

    def __init__(self, session_manager: Database | None = None) -> None:
        self._db = session_manager or get_session_manager()
        self._import_job_repo = get_import_job_repository(self._db)
        self._bookmark_import_repo = get_bookmark_import_repository(self._db)
        self._user_content_repo = UserContentRepositoryAdapter(self._db)

    async def create_import_job(
        self,
        *,
        user_id: int,
        source_format: str,
        file_name: str | None,
        total_items: int,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        job = await self._import_job_repo.async_create_job(
            user_id=user_id,
            source_format=source_format,
            file_name=file_name,
            total_items=total_items,
            options=options,
        )
        return self._job_to_response(job).model_dump(by_alias=True)

    async def get_import_job(self, *, job_id: int, user_id: int) -> dict[str, Any]:
        job = await self._verify_job_ownership(job_id=job_id, user_id=user_id)
        return self._job_to_response(job).model_dump(by_alias=True)

    async def list_import_jobs(self, *, user_id: int) -> list[dict[str, Any]]:
        jobs = await self._import_job_repo.async_list_jobs(user_id)
        return [self._job_to_response(job).model_dump(by_alias=True) for job in jobs]

    async def delete_import_job(self, *, job_id: int, user_id: int) -> None:
        await self._verify_job_ownership(job_id=job_id, user_id=user_id)
        await self._import_job_repo.async_delete_job(job_id)

    async def export_summaries(
        self,
        *,
        user_id: int,
        tag: str | None,
        collection_id: int | None,
    ) -> list[dict[str, Any]]:
        """Return serialized summary rows for bookmark export."""
        return await self._user_content_repo.async_export_summaries(
            user_id=user_id,
            tag=tag,
            collection_id=collection_id,
        )

    async def _verify_job_ownership(self, *, job_id: int, user_id: int) -> dict[str, Any]:
        job = await self._import_job_repo.async_get_job(job_id)
        if job is None:
            raise ResourceNotFoundError("ImportJob", job_id)
        if job["user"] != user_id:
            raise ResourceNotFoundError("ImportJob", job_id)
        return cast("dict[str, Any]", job)

    @staticmethod
    def _job_to_response(job: dict[str, Any]) -> ImportJobResponse:
        return ImportJobResponse(
            id=job["id"],
            source_format=job["source_format"],
            file_name=job.get("file_name"),
            status=job["status"],
            total_items=job["total_items"],
            processed_items=job["processed_items"],
            created_items=job["created_items"],
            skipped_items=job["skipped_items"],
            failed_items=job["failed_items"],
            errors=job.get("errors_json") or [],
            created_at=isotime(job["created_at"]),
            updated_at=isotime(job["updated_at"]),
        )
