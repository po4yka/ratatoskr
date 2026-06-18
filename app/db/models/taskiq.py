"""Taskiq operational SQLAlchemy models."""

from __future__ import annotations

import datetime as dt  # noqa: TC003

from sqlalchemy import DateTime, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JSONB, JSONValue, _utcnow


class TaskiqFailedJob(Base):
    __tablename__ = "taskiq_failed_jobs"
    __table_args__ = (
        Index("ix_taskiq_failed_jobs_task_name_last_failed_at", "task_name", "last_failed_at"),
        Index("ix_taskiq_failed_jobs_status_last_failed_at", "status", "last_failed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_name: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    args_json: Mapped[JSONValue] = mapped_column(JSONB, default=list, nullable=False)
    kwargs_json: Mapped[JSONValue] = mapped_column(JSONB, default=dict, nullable=False)
    labels_json: Mapped[JSONValue] = mapped_column(JSONB, default=dict, nullable=False)
    traceback_text: Mapped[str] = mapped_column(Text, nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="dead_letter", nullable=False)
    last_failed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    requeued_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


TASKIQ_MODELS: tuple[type[Base], ...] = (TaskiqFailedJob,)


__all__ = ["TASKIQ_MODELS", "TaskiqFailedJob"]
