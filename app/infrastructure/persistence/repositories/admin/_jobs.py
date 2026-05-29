"""Admin read queries for job pipeline health and content failure tracking."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.db.models import ImportJob, Request, Summary
from app.infrastructure.persistence.repositories.admin._helpers import isotime

if TYPE_CHECKING:
    from app.db.session import Database


class JobsReadRepository:
    """Read-side queries for pipeline job status and content health."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_job_status(self, *, today: Any) -> dict[str, Any]:
        async with self._database.session() as session:
            pending = await session.scalar(
                select(func.count(Request.id)).where(Request.status == "pending")
            )
            processing = await session.scalar(
                select(func.count(Request.id)).where(
                    Request.status.in_(["crawling", "summarizing", "processing"])
                )
            )
            completed_today = await session.scalar(
                select(func.count(Request.id)).where(
                    Request.status == "completed", Request.updated_at >= today
                )
            )
            failed_today = await session.scalar(
                select(func.count(Request.id)).where(
                    Request.status == "error", Request.updated_at >= today
                )
            )
            imports_active = await session.scalar(
                select(func.count(ImportJob.id)).where(ImportJob.status == "processing")
            )
            imports_completed_today = await session.scalar(
                select(func.count(ImportJob.id)).where(
                    ImportJob.status == "completed", ImportJob.updated_at >= today
                )
            )
            return {
                "pipeline": {
                    "pending": int(pending or 0),
                    "processing": int(processing or 0),
                    "completed_today": int(completed_today or 0),
                    "failed_today": int(failed_today or 0),
                },
                "imports": {
                    "active": int(imports_active or 0),
                    "completed_today": int(imports_completed_today or 0),
                },
            }

    async def async_content_health(self) -> dict[str, Any]:
        async with self._database.session() as session:
            total_summaries = await session.scalar(select(func.count(Summary.id)))
            total_requests = await session.scalar(select(func.count(Request.id)))
            failed_requests = await session.scalar(
                select(func.count(Request.id)).where(Request.status == "error")
            )

            failed_by_error_type: dict[str, int] = {}
            error_groups = await session.execute(
                select(Request.error_type, func.count(Request.id))
                .where(Request.status == "error")
                .group_by(Request.error_type)
            )
            for error_type, count in error_groups:
                key = error_type or "unknown"
                failed_by_error_type[key] = int(count or 0)

            recent_failures: list[dict[str, Any]] = []
            failures = (
                await session.execute(
                    select(Request)
                    .where(Request.status == "error")
                    .order_by(Request.created_at.desc())
                    .limit(20)
                )
            ).scalars()
            for request in failures:
                recent_failures.append(
                    {
                        "id": request.id,
                        "url": request.input_url,
                        "error_type": request.error_type,
                        "error_message": request.error_message,
                        "created_at": isotime(request.created_at),
                    }
                )

            return {
                "total_summaries": int(total_summaries or 0),
                "total_requests": int(total_requests or 0),
                "failed_requests": int(failed_requests or 0),
                "failed_by_error_type": failed_by_error_type,
                "recent_failures": recent_failures,
            }
