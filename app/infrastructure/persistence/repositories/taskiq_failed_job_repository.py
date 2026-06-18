"""Repository for Taskiq failed-job dead-letter rows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.json_utils import prepare_json_payload
from app.db.models import TaskiqFailedJob, model_to_dict
from app.db.types import _utcnow

if TYPE_CHECKING:
    from app.db.session import Database


class TaskiqFailedJobRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_insert_failed_job(
        self,
        *,
        task_name: str,
        task_id: str | None,
        args: list[Any],
        kwargs: dict[str, Any],
        labels: dict[str, Any],
        traceback_text: str,
        error_text: str | None,
        attempt_count: int,
    ) -> int:
        async with self._database.transaction() as session:
            row = TaskiqFailedJob(
                task_name=task_name,
                task_id=task_id,
                args_json=prepare_json_payload(args, default=[]),
                kwargs_json=prepare_json_payload(kwargs, default={}),
                labels_json=prepare_json_payload(labels, default={}),
                traceback_text=traceback_text,
                error_text=error_text,
                attempt_count=max(1, attempt_count),
                status="dead_letter",
                last_failed_at=_utcnow(),
            )
            session.add(row)
            await session.flush()
            return row.id

    async def async_get_failed_job(self, failed_job_id: int) -> dict[str, Any] | None:
        async with self._database.session() as session:
            row = await session.scalar(
                select(TaskiqFailedJob).where(TaskiqFailedJob.id == failed_job_id)
            )
            return model_to_dict(row)

    async def async_mark_requeued(self, failed_job_id: int) -> None:
        async with self._database.transaction() as session:
            row = await session.scalar(
                select(TaskiqFailedJob).where(TaskiqFailedJob.id == failed_job_id)
            )
            if row is None:
                return
            row.status = "requeued"
            row.requeued_at = _utcnow()


__all__ = ["TaskiqFailedJobRepository"]
