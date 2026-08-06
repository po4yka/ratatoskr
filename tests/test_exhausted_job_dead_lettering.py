"""Coverage for the terminal statuses `reconcile_startup`'s sweeps write.

A job that runs out of attempts has to reach `dead_letter`, and its request has
to reach a terminal status with it -- the invariant `mark_failed` and
`recover_interrupted_synchronous_requests` already hold.

The sweep used to match an allowlist of `queued`, `failed`, `running`, which
omitted `pending`. But `reclaim_orphaned_worker_leases`, `release_lease` and
`requeue_expired_leases` all write `pending` without resetting `attempt_count`,
and `lease_next` only takes rows with `attempt_count < max_attempts`. A job that
landed in `pending` on its final attempt was therefore unleasable and unreachable
by the sweep, and sat there permanently with its request never leaving `pending`
(production: job 352 / request 1597, `WORKER_RESTARTED`, stuck for three months).

These drive the real UPDATE ... FROM against Postgres -- a correlated update
cannot be meaningfully exercised through a mocked session -- so they need
`TEST_DATABASE_URL` and skip cleanly without it (see tests/conftest.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, update

from app.core.time_utils import UTC
from app.db.models import Request, RequestProcessingJob
from app.infrastructure.persistence.request_processing_job_repository import (
    RequestProcessingJobRepository,
)
from tests.db_helpers_async import create_request, insert_summary

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.session import Database


async def _insert_job(
    session: AsyncSession,
    *,
    request_id: int,
    status: str,
    attempt_count: int,
    max_attempts: int = 3,
    last_error_code: str | None = None,
    last_error_message: str | None = None,
) -> None:
    session.add(
        RequestProcessingJob(
            request_id=request_id,
            status=status,
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            last_error_code=last_error_code,
            last_error_message=last_error_message,
        )
    )
    await session.flush()


async def _job_for(session: AsyncSession, request_id: int) -> RequestProcessingJob:
    job = await session.scalar(
        select(RequestProcessingJob).where(RequestProcessingJob.request_id == request_id)
    )
    assert job is not None
    return job


async def _request(session: AsyncSession, request_id: int) -> Request:
    row = await session.scalar(select(Request).where(Request.id == request_id))
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_dead_letters_exhausted_pending_job_and_fails_its_request(
    database: Database, session: AsyncSession
) -> None:
    """The production stall: `pending` at the attempt ceiling after a worker died."""
    request_id = await create_request(
        session,
        type_="url",
        status="pending",
        correlation_id="cid-stranded",
        chat_id=555,
        user_id=1,
        input_url="https://example.com/stranded",
    )
    await _insert_job(
        session,
        request_id=request_id,
        status="pending",
        attempt_count=3,
        max_attempts=3,
        last_error_code="WORKER_RESTARTED",
        last_error_message="Worker process died holding this lease",
    )
    await session.commit()

    swept = await RequestProcessingJobRepository(database).dead_letter_exhausted()
    assert swept == 1

    session.expire_all()
    job = await _job_for(session, request_id)
    assert job.status == "dead_letter"
    assert job.lease_owner is None
    assert job.retry_after is None

    request = await _request(session, request_id)
    assert request.status == "error"
    # The request carries the code its own job recorded, not a flat label.
    assert request.error_type == "WORKER_RESTARTED"
    assert request.error_message == "Worker process died holding this lease"
    assert request.error_timestamp is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("job_status", ["queued", "failed", "running"])
async def test_still_dead_letters_the_previously_covered_statuses(
    database: Database, session: AsyncSession, job_status: str
) -> None:
    """Widening the predicate must not drop what the allowlist already caught."""
    request_id = await create_request(
        session,
        type_="url",
        status="processing",
        correlation_id=f"cid-{job_status}",
        chat_id=555,
        user_id=1,
        input_url=f"https://example.com/{job_status}",
    )
    await _insert_job(
        session,
        request_id=request_id,
        status=job_status,
        attempt_count=3,
        max_attempts=3,
        last_error_code="LEASE_EXPIRED",
    )
    await session.commit()

    swept = await RequestProcessingJobRepository(database).dead_letter_exhausted()
    assert swept == 1

    session.expire_all()
    assert (await _job_for(session, request_id)).status == "dead_letter"
    assert (await _request(session, request_id)).status == "error"


@pytest.mark.asyncio
async def test_leaves_a_job_with_attempts_remaining_alone(
    database: Database, session: AsyncSession
) -> None:
    request_id = await create_request(
        session,
        type_="url",
        status="pending",
        correlation_id="cid-retryable",
        chat_id=555,
        user_id=1,
        input_url="https://example.com/retryable",
    )
    await _insert_job(session, request_id=request_id, status="pending", attempt_count=1)
    await session.commit()

    assert await RequestProcessingJobRepository(database).dead_letter_exhausted() == 0

    session.expire_all()
    assert (await _job_for(session, request_id)).status == "pending"
    assert (await _request(session, request_id)).status == "pending"


@pytest.mark.asyncio
async def test_does_not_overwrite_a_request_that_already_finished(
    database: Database, session: AsyncSession
) -> None:
    """A late summary can land before the sweep runs; `ok` must not become `error`."""
    request_id = await create_request(
        session,
        type_="url",
        status="ok",
        correlation_id="cid-late-success",
        chat_id=555,
        user_id=1,
        input_url="https://example.com/late-success",
    )
    await _insert_job(
        session,
        request_id=request_id,
        status="pending",
        attempt_count=3,
        max_attempts=3,
        last_error_code="WORKER_RESTARTED",
    )
    await session.commit()

    await RequestProcessingJobRepository(database).dead_letter_exhausted()

    session.expire_all()
    # The job is retired either way -- it genuinely cannot run again.
    assert (await _job_for(session, request_id)).status == "dead_letter"
    request = await _request(session, request_id)
    assert request.status == "ok"
    assert request.error_type is None


@pytest.mark.asyncio
async def test_sweeping_twice_is_a_no_op(database: Database, session: AsyncSession) -> None:
    """`dead_letter` is terminal, so a second startup must not re-report the row."""
    request_id = await create_request(
        session,
        type_="url",
        status="pending",
        correlation_id="cid-idempotent",
        chat_id=555,
        user_id=1,
        input_url="https://example.com/idempotent",
    )
    await _insert_job(
        session,
        request_id=request_id,
        status="pending",
        attempt_count=3,
        max_attempts=3,
        last_error_code="WORKER_RESTARTED",
    )
    await session.commit()

    repo = RequestProcessingJobRepository(database)
    assert await repo.dead_letter_exhausted() == 1
    assert await repo.dead_letter_exhausted() == 0


@pytest.mark.asyncio
async def test_reconcile_completes_a_stale_request_with_the_canonical_status(
    database: Database, session: AsyncSession
) -> None:
    """`reconcile_stuck_processing_requests` wrote 'success', which is not a status.

    `RequestStatus` has no such member -- COMPLETED is 'ok' -- so the rows it
    produced were not terminal to the guard in
    `recover_interrupted_synchronous_requests` and had to be normalised as a
    legacy value on every API read. Production carried 7 of them.
    """
    request_id = await create_request(
        session,
        type_="url",
        status="processing",
        correlation_id="cid-stale-done",
        chat_id=555,
        user_id=1,
        input_url="https://example.com/stale-done",
    )
    await _insert_job(session, request_id=request_id, status="failed", attempt_count=1)
    await insert_summary(session, request_id=request_id)
    # The sweep only considers requests untouched for longer than the cutoff.
    await session.execute(
        update(Request)
        .where(Request.id == request_id)
        .values(updated_at=datetime.now(UTC) - timedelta(hours=2))
    )
    await session.commit()

    reconciled = await RequestProcessingJobRepository(database).reconcile_stuck_processing_requests(
        older_than_seconds=900,
        max_attempts=3,
    )
    assert reconciled == 1

    session.expire_all()
    assert (await _request(session, request_id)).status == "ok"
    assert (await _job_for(session, request_id)).status == "succeeded"
