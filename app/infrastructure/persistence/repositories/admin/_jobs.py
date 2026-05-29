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
            # Four Request counts share the same base table; combine with FILTER aggregates.
            request_row = (
                await session.execute(
                    select(
                        func.count(Request.id).filter(Request.status == "pending").label("pending"),
                        func.count(Request.id)
                        .filter(Request.status.in_(["crawling", "summarizing", "processing"]))
                        .label("processing"),
                        func.count(Request.id)
                        .filter(
                            Request.status == "completed",
                            Request.updated_at >= today,
                        )
                        .label("completed_today"),
                        func.count(Request.id)
                        .filter(
                            Request.status == "error",
                            Request.updated_at >= today,
                        )
                        .label("failed_today"),
                    )
                )
            ).one()
            pending, processing, completed_today, failed_today = request_row

            # Two ImportJob counts share the same base table; combine with FILTER aggregates.
            import_row = (
                await session.execute(
                    select(
                        func.count(ImportJob.id)
                        .filter(ImportJob.status == "processing")
                        .label("imports_active"),
                        func.count(ImportJob.id)
                        .filter(
                            ImportJob.status == "completed",
                            ImportJob.updated_at >= today,
                        )
                        .label("imports_completed_today"),
                    )
                )
            ).one()
            imports_active, imports_completed_today = import_row

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

            # total_requests and failed_requests both scan Request; combine with FILTER.
            request_row = (
                await session.execute(
                    select(
                        func.count(Request.id).label("total_requests"),
                        func.count(Request.id)
                        .filter(Request.status == "error")
                        .label("failed_requests"),
                    )
                )
            ).one()
            total_requests, failed_requests = request_row

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
