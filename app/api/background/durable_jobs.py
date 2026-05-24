from __future__ import annotations

import asyncio
import socket
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.core.logging_utils import get_logger, log_exception
from app.db.models import Request, RequestProcessingJob, Summary, model_to_dict
from app.db.types import _utcnow

logger = get_logger(__name__)

TERMINAL_JOB_STATUSES = {"succeeded", "dead_letter"}


@dataclass(frozen=True, slots=True)
class LeasedRequestJob:
    id: int
    request_id: int
    attempt_count: int
    max_attempts: int
    correlation_id: str | None


class RequestProcessingJobRepository:
    """Durable request-processing job store with DB leases."""

    def __init__(self, database: Any) -> None:
        self._database = database

    async def enqueue(
        self,
        *,
        request_id: int,
        correlation_id: str | None,
        max_attempts: int,
    ) -> dict[str, Any]:
        now = _utcnow()
        values = {
            "request_id": request_id,
            "status": "queued",
            "attempt_count": 0,
            "max_attempts": max_attempts,
            "lease_owner": None,
            "lease_expires_at": None,
            "retry_after": now,
            "last_error_code": None,
            "last_error_message": None,
            "correlation_id": correlation_id,
            "updated_at": now,
            "created_at": now,
        }
        async with self._database.transaction() as session:
            stmt = (
                insert(RequestProcessingJob)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[RequestProcessingJob.request_id],
                    set_={
                        "status": "queued",
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "retry_after": now,
                        "last_error_code": None,
                        "last_error_message": None,
                        "correlation_id": correlation_id,
                        "max_attempts": max_attempts,
                        "updated_at": now,
                    },
                    where=RequestProcessingJob.status.notin_(TERMINAL_JOB_STATUSES),
                )
                .returning(RequestProcessingJob)
            )
            row = await session.scalar(stmt)
            if row is None:
                row = await session.scalar(
                    select(RequestProcessingJob).where(
                        RequestProcessingJob.request_id == request_id
                    )
                )
            if row is None:
                msg = f"request processing job enqueue failed for request_id={request_id}"
                raise RuntimeError(msg)
            return model_to_dict(row) or {}

    async def lease_next(
        self,
        *,
        lease_owner: str,
        lease_ttl_seconds: int,
    ) -> LeasedRequestJob | None:
        now = _utcnow()
        lease_expires_at = now + timedelta(seconds=lease_ttl_seconds)
        async with self._database.transaction() as session:
            stmt: Select[tuple[RequestProcessingJob]] = (
                select(RequestProcessingJob)
                .where(
                    or_(
                        RequestProcessingJob.status == "queued",
                        (
                            (RequestProcessingJob.status == "failed")
                            & (
                                (RequestProcessingJob.retry_after.is_(None))
                                | (RequestProcessingJob.retry_after <= now)
                            )
                        ),
                        (
                            (RequestProcessingJob.status == "running")
                            & (RequestProcessingJob.lease_expires_at <= now)
                        ),
                    ),
                    RequestProcessingJob.attempt_count < RequestProcessingJob.max_attempts,
                )
                .order_by(
                    RequestProcessingJob.retry_after.asc().nullsfirst(), RequestProcessingJob.id
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            job = await session.scalar(stmt)
            if job is None:
                return None
            job.status = "running"
            job.lease_owner = lease_owner
            job.lease_expires_at = lease_expires_at
            job.attempt_count += 1
            job.updated_at = now
            await session.flush()
            return LeasedRequestJob(
                id=job.id,
                request_id=job.request_id,
                attempt_count=job.attempt_count,
                max_attempts=job.max_attempts,
                correlation_id=job.correlation_id,
            )

    async def mark_succeeded(
        self,
        job_id: int,
        *,
        lease_owner: str,
        request_id: int | None = None,
    ) -> None:
        now = _utcnow()
        async with self._database.transaction() as session:
            await session.execute(
                update(RequestProcessingJob)
                .where(
                    RequestProcessingJob.id == job_id,
                    RequestProcessingJob.lease_owner == lease_owner,
                )
                .values(
                    status="succeeded",
                    lease_owner=None,
                    lease_expires_at=None,
                    retry_after=None,
                    last_error_code=None,
                    last_error_message=None,
                    updated_at=now,
                )
            )
            if request_id is not None:
                await session.execute(
                    update(Request)
                    .where(Request.id == request_id)
                    .values(
                        status="success",
                        error_type=None,
                        error_message=None,
                        error_timestamp=None,
                        updated_at=now,
                    )
                )

    async def mark_failed(
        self,
        job: LeasedRequestJob,
        *,
        lease_owner: str,
        error_code: str,
        error_message: str,
        retry_delay_seconds: int,
    ) -> str:
        now = _utcnow()
        terminal = job.attempt_count >= job.max_attempts
        status = "dead_letter" if terminal else "failed"
        message = error_message[:2000]
        retry_after = None if terminal else now + timedelta(seconds=retry_delay_seconds)
        async with self._database.transaction() as session:
            await session.execute(
                update(RequestProcessingJob)
                .where(
                    RequestProcessingJob.id == job.id,
                    RequestProcessingJob.lease_owner == lease_owner,
                )
                .values(
                    status=status,
                    lease_owner=None,
                    lease_expires_at=None,
                    retry_after=retry_after,
                    last_error_code=error_code,
                    last_error_message=message,
                    updated_at=now,
                )
            )
            await session.execute(
                update(Request)
                .where(Request.id == job.request_id)
                .values(
                    status="error",
                    error_type=error_code,
                    error_message=message,
                    error_timestamp=now,
                    updated_at=now,
                )
            )
        return status

    async def record_synchronous_start(
        self,
        *,
        request_id: int,
        correlation_id: str | None,
        lease_owner: str = "bot:sync",
        lease_ttl_seconds: int = 900,
    ) -> None:
        """Insert/update a `running` row for a synchronous bot run.

        Worker's reconcile_stuck_processing_requests reaps rows where the
        lease expired without a terminal status, providing crash-recovery
        for the synchronous bot path. The lease TTL is sized to the
        maximum URL-flow runtime (15 min default).
        """
        now = _utcnow()
        lease_expires_at = now + timedelta(seconds=lease_ttl_seconds)
        base_values = {
            "request_id": request_id,
            "status": "running",
            "attempt_count": 1,
            "max_attempts": 1,
            "lease_owner": lease_owner,
            "lease_expires_at": lease_expires_at,
            "retry_after": None,
            "last_error_code": None,
            "last_error_message": None,
            "correlation_id": correlation_id,
            "updated_at": now,
            "created_at": now,
        }
        async with self._database.transaction() as session:
            stmt = (
                insert(RequestProcessingJob)
                .values(**base_values)
                .on_conflict_do_update(
                    index_elements=[RequestProcessingJob.request_id],
                    set_={
                        "status": "running",
                        "lease_owner": lease_owner,
                        "lease_expires_at": lease_expires_at,
                        "updated_at": now,
                    },
                    where=RequestProcessingJob.status.notin_(TERMINAL_JOB_STATUSES),
                )
            )
            await session.execute(stmt)

    async def record_synchronous_outcome(
        self,
        *,
        request_id: int,
        correlation_id: str | None,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Insert or update a terminal job row for an in-process synchronous run.

        Bot-originated URL requests execute synchronously inside the Telethon
        event loop and never pass through the worker poll/lease lifecycle, so
        they would otherwise leave no row in ``request_processing_jobs``. This
        method writes a single terminal row (``succeeded`` | ``failed`` |
        ``dead_letter``) so failed bot requests show up in the same operator
        view as worker-driven jobs. It does NOT change the execution model;
        retries remain unavailable for synchronous runs until a separate PR
        moves bot execution onto the worker.
        """
        if status not in {"succeeded", "failed", "dead_letter"}:
            msg = f"record_synchronous_outcome: invalid status {status!r}"
            raise ValueError(msg)
        now = _utcnow()
        truncated_message = (error_message or "")[:2000] or None
        base_values = {
            "request_id": request_id,
            "status": status,
            "attempt_count": 1,
            "max_attempts": 1,
            "lease_owner": None,
            "lease_expires_at": None,
            "retry_after": None,
            "last_error_code": error_code,
            "last_error_message": truncated_message,
            "correlation_id": correlation_id,
            "updated_at": now,
            "created_at": now,
        }
        async with self._database.transaction() as session:
            stmt = (
                insert(RequestProcessingJob)
                .values(**base_values)
                .on_conflict_do_update(
                    index_elements=[RequestProcessingJob.request_id],
                    set_={
                        "status": status,
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "retry_after": None,
                        "last_error_code": error_code,
                        "last_error_message": truncated_message,
                        "correlation_id": correlation_id,
                        "updated_at": now,
                    },
                )
            )
            await session.execute(stmt)

    async def requeue_expired_leases(self) -> int:
        now = _utcnow()
        async with self._database.transaction() as session:
            result = await session.execute(
                update(RequestProcessingJob)
                .where(
                    RequestProcessingJob.status == "running",
                    RequestProcessingJob.lease_expires_at.is_not(None),
                    RequestProcessingJob.lease_expires_at <= now,
                    RequestProcessingJob.attempt_count < RequestProcessingJob.max_attempts,
                )
                .values(
                    status="queued",
                    lease_owner=None,
                    lease_expires_at=None,
                    retry_after=now,
                    last_error_code="LEASE_EXPIRED",
                    last_error_message="Worker lease expired before completion",
                    updated_at=now,
                )
            )
            return int(result.rowcount or 0)

    async def dead_letter_exhausted(self) -> int:
        now = _utcnow()
        async with self._database.transaction() as session:
            result = await session.execute(
                update(RequestProcessingJob)
                .where(
                    RequestProcessingJob.status.in_(("queued", "failed", "running")),
                    RequestProcessingJob.attempt_count >= RequestProcessingJob.max_attempts,
                )
                .values(
                    status="dead_letter",
                    lease_owner=None,
                    lease_expires_at=None,
                    retry_after=None,
                    updated_at=now,
                )
            )
            return int(result.rowcount or 0)

    async def reconcile_stuck_processing_requests(
        self,
        *,
        older_than_seconds: int,
        max_attempts: int,
    ) -> int:
        now = _utcnow()
        cutoff = now - timedelta(seconds=older_than_seconds)
        async with self._database.transaction() as session:
            rows = await session.execute(
                select(Request.id, Request.correlation_id, Summary.id.label("summary_id"))
                .outerjoin(Summary, Summary.request_id == Request.id)
                .outerjoin(RequestProcessingJob, RequestProcessingJob.request_id == Request.id)
                .where(
                    Request.status == "processing",
                    Request.updated_at <= cutoff,
                    or_(
                        RequestProcessingJob.id.is_(None),
                        RequestProcessingJob.status.in_(("failed", "dead_letter")),
                    ),
                )
                .limit(200)
            )
            stale_rows = list(rows)
            if not stale_rows:
                return 0

            succeeded_jobs: list[dict[str, Any]] = []
            queued_jobs: list[dict[str, Any]] = []
            succeeded_request_ids: list[int] = []
            for request_id, correlation_id, summary_id in stale_rows:
                if summary_id is not None:
                    succeeded_request_ids.append(request_id)
                    succeeded_jobs.append(
                        {
                            "request_id": request_id,
                            "status": "succeeded",
                            "attempt_count": 0,
                            "max_attempts": max_attempts,
                            "correlation_id": correlation_id,
                            "retry_after": None,
                            "updated_at": now,
                            "created_at": now,
                        }
                    )
                    continue
                queued_jobs.append(
                    {
                        "request_id": request_id,
                        "status": "queued",
                        "attempt_count": 0,
                        "max_attempts": max_attempts,
                        "correlation_id": correlation_id,
                        "retry_after": now,
                        "updated_at": now,
                        "created_at": now,
                    }
                )

            if succeeded_request_ids:
                await session.execute(
                    update(Request)
                    .where(Request.id.in_(succeeded_request_ids))
                    .values(status="success", updated_at=now)
                )
            if succeeded_jobs:
                await session.execute(
                    insert(RequestProcessingJob)
                    .values(succeeded_jobs)
                    .on_conflict_do_update(
                        index_elements=[RequestProcessingJob.request_id],
                        set_={"status": "succeeded", "updated_at": now},
                    )
                )
            if queued_jobs:
                await session.execute(
                    insert(RequestProcessingJob)
                    .values(queued_jobs)
                    .on_conflict_do_update(
                        index_elements=[RequestProcessingJob.request_id],
                        set_={
                            "status": "queued",
                            "lease_owner": None,
                            "lease_expires_at": None,
                            "retry_after": now,
                            "max_attempts": max_attempts,
                            "updated_at": now,
                        },
                    )
                )
            return len(stale_rows)

    async def has_summary(self, request_id: int) -> bool:
        async with self._database.session() as session:
            return bool(
                await session.scalar(select(Summary.id).where(Summary.request_id == request_id))
            )

    async def get_request_status(self, request_id: int) -> tuple[str | None, str | None]:
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(Request.status, Request.error_message).where(Request.id == request_id)
                )
            ).first()
            if row is None:
                return None, "Request not found"
            return row[0], row[1]

    async def pending_count(self) -> int:
        now = _utcnow()
        async with self._database.session() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(RequestProcessingJob)
                    .where(
                        or_(
                            RequestProcessingJob.status == "queued",
                            (
                                (RequestProcessingJob.status == "failed")
                                & (
                                    (RequestProcessingJob.retry_after.is_(None))
                                    | (RequestProcessingJob.retry_after <= now)
                                )
                            ),
                        )
                    )
                )
                or 0
            )


class DurableRequestProcessingQueue:
    """Durable enqueue and worker loop for request processing."""

    def __init__(
        self,
        *,
        repository: RequestProcessingJobRepository,
        processor: Any,
        max_attempts: int,
        lease_ttl_seconds: int = 300,
        retry_delay_seconds: int = 30,
        poll_interval_seconds: float = 1.0,
        stale_processing_seconds: int = 900,
    ) -> None:
        self._repo = repository
        self._processor = processor
        self._max_attempts = max_attempts
        self._lease_ttl_seconds = lease_ttl_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._stale_processing_seconds = stale_processing_seconds
        self._owner = f"{socket.gethostname()}:{uuid.uuid4().hex}"
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def enqueue(
        self, *, request_id: int, correlation_id: str | None = None
    ) -> dict[str, Any]:
        return await self._repo.enqueue(
            request_id=request_id,
            correlation_id=correlation_id,
            max_attempts=self._max_attempts,
        )

    async def reconcile_startup(self) -> dict[str, int]:
        requeued = await self._repo.requeue_expired_leases()
        dead_lettered = await self._repo.dead_letter_exhausted()
        stuck = await self._repo.reconcile_stuck_processing_requests(
            older_than_seconds=self._stale_processing_seconds,
            max_attempts=self._max_attempts,
        )
        logger.info(
            "durable_request_processing_reconciled",
            extra={"requeued": requeued, "dead_lettered": dead_lettered, "stuck": stuck},
        )
        return {"requeued": requeued, "dead_lettered": dead_lettered, "stuck": stuck}

    async def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run_forever(), name="request-processing-jobs")
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def run_once(self) -> bool:
        job = await self._repo.lease_next(
            lease_owner=self._owner,
            lease_ttl_seconds=self._lease_ttl_seconds,
        )
        if job is None:
            return False
        await self._process_leased_job(job)
        return True

    async def run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_exception(logger, "durable_request_processing_worker_failed", exc)
                processed = False
            if not processed:
                await asyncio.sleep(self._poll_interval_seconds)

    async def _process_leased_job(self, job: LeasedRequestJob) -> None:
        try:
            if await self._repo.has_summary(job.request_id):
                await self._repo.mark_succeeded(
                    job.id,
                    lease_owner=self._owner,
                    request_id=job.request_id,
                )
                return
            await self._processor.execute_request(
                job.request_id,
                correlation_id=job.correlation_id,
            )
            if await self._repo.has_summary(job.request_id):
                await self._repo.mark_succeeded(
                    job.id,
                    lease_owner=self._owner,
                    request_id=job.request_id,
                )
                return
            request_status, error_message = await self._repo.get_request_status(job.request_id)
            if request_status in {"success", "complete", "completed", "ok"}:
                await self._repo.mark_succeeded(job.id, lease_owner=self._owner)
                return
            await self._repo.mark_failed(
                job,
                lease_owner=self._owner,
                error_code="PROCESSING_FAILED",
                error_message=error_message or f"Request ended with status {request_status}",
                retry_delay_seconds=self._retry_delay_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._repo.mark_failed(
                job,
                lease_owner=self._owner,
                error_code=exc.__class__.__name__,
                error_message=str(exc),
                retry_delay_seconds=self._retry_delay_seconds,
            )
            log_exception(
                logger,
                "durable_request_processing_job_failed",
                exc,
                request_id=job.request_id,
                job_id=job.id,
            )
