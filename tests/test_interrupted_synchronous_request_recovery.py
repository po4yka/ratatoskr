"""Coverage for RequestProcessingJobRepository.recover_interrupted_synchronous_requests.

Bot-originated URL requests run synchronously in-process and record a
`bot:sync` lease via `record_synchronous_start`. If the bot process dies
mid-flight (e.g. a cgroup OOM restart), the request is left stuck: the
request row never reaches a terminal status and the job row is stuck
`running` with an expired lease. `attempt_count(1) < max_attempts(1)` being
false means `requeue_expired_leases` can never reclaim it, and no worker path
can resume an in-process synchronous run anyway. `recover_interrupted_synchronous_requests`
detects those orphans on bot startup, marks both rows terminal, and returns
lightweight records so the caller can notify the owner to resend the link.

These tests drive the real query end-to-end against Postgres (the join +
filter logic cannot be meaningfully exercised via a mocked session), so they
require `TEST_DATABASE_URL` and pytest.skip() cleanly without it (see
tests/conftest.py's `database` / `session` fixtures).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.core.time_utils import UTC
from app.db.models import Request, RequestProcessingJob
from app.infrastructure.persistence.request_processing_job_repository import (
    InterruptedRequest,
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
    lease_owner: str | None,
    lease_expires_at: datetime | None,
    correlation_id: str | None,
    attempt_count: int = 1,
    max_attempts: int = 1,
) -> None:
    session.add(
        RequestProcessingJob(
            request_id=request_id,
            status=status,
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
            correlation_id=correlation_id,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_recovers_orphaned_synchronous_request(
    database: Database, session: AsyncSession
) -> None:
    """A non-terminal request with an expired bot:sync lease and no summary is recovered."""
    request_id = await create_request(
        session,
        type_="url",
        status="crawling",
        correlation_id="cid-orphan",
        chat_id=555,
        user_id=1,
        input_url="https://example.com/orphan",
    )
    await _insert_job(
        session,
        request_id=request_id,
        status="running",
        lease_owner="bot:sync",
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
        correlation_id="cid-orphan",
    )
    await session.commit()

    repo = RequestProcessingJobRepository(database)
    recovered = await repo.recover_interrupted_synchronous_requests()

    assert recovered == [
        InterruptedRequest(
            request_id=request_id,
            chat_id=555,
            input_url="https://example.com/orphan",
            correlation_id="cid-orphan",
        )
    ]

    refreshed_request = await session.scalar(select(Request).where(Request.id == request_id))
    assert refreshed_request is not None
    assert refreshed_request.status == "error"
    assert refreshed_request.error_type == "processing_interrupted"
    assert refreshed_request.error_message == (
        "Processing was interrupted by a bot restart before completion."
    )
    assert refreshed_request.error_timestamp is not None

    refreshed_job = await session.scalar(
        select(RequestProcessingJob).where(RequestProcessingJob.request_id == request_id)
    )
    assert refreshed_job is not None
    assert refreshed_job.status == "dead_letter"
    assert refreshed_job.lease_owner is None
    assert refreshed_job.lease_expires_at is None
    assert refreshed_job.last_error_code == "INTERRUPTED"


@pytest.mark.asyncio
async def test_does_not_touch_request_that_already_has_a_summary(
    database: Database, session: AsyncSession
) -> None:
    """Same orphan shape, but a summary already exists: must be left alone."""
    request_id = await create_request(
        session,
        type_="url",
        status="crawling",
        correlation_id="cid-has-summary",
        chat_id=555,
        user_id=1,
        input_url="https://example.com/has-summary",
    )
    await _insert_job(
        session,
        request_id=request_id,
        status="running",
        lease_owner="bot:sync",
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
        correlation_id="cid-has-summary",
    )
    await insert_summary(session, request_id=request_id)
    await session.commit()

    repo = RequestProcessingJobRepository(database)
    recovered = await repo.recover_interrupted_synchronous_requests()

    assert recovered == []

    refreshed_request = await session.scalar(select(Request).where(Request.id == request_id))
    assert refreshed_request is not None
    assert refreshed_request.status == "crawling"

    refreshed_job = await session.scalar(
        select(RequestProcessingJob).where(RequestProcessingJob.request_id == request_id)
    )
    assert refreshed_job is not None
    assert refreshed_job.status == "running"
    assert refreshed_job.lease_owner == "bot:sync"


@pytest.mark.asyncio
async def test_does_not_touch_running_job_with_unexpired_lease(
    database: Database, session: AsyncSession
) -> None:
    """Same orphan shape, but the lease has not expired yet: must be left alone."""
    request_id = await create_request(
        session,
        type_="url",
        status="crawling",
        correlation_id="cid-active",
        chat_id=555,
        user_id=1,
        input_url="https://example.com/active",
    )
    await _insert_job(
        session,
        request_id=request_id,
        status="running",
        lease_owner="bot:sync",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        correlation_id="cid-active",
    )
    await session.commit()

    repo = RequestProcessingJobRepository(database)
    recovered = await repo.recover_interrupted_synchronous_requests()

    assert recovered == []

    refreshed_request = await session.scalar(select(Request).where(Request.id == request_id))
    assert refreshed_request is not None
    assert refreshed_request.status == "crawling"

    refreshed_job = await session.scalar(
        select(RequestProcessingJob).where(RequestProcessingJob.request_id == request_id)
    )
    assert refreshed_job is not None
    assert refreshed_job.status == "running"
    assert refreshed_job.lease_owner == "bot:sync"
