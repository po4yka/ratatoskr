"""Coverage for the two consumers of ``request_processing_jobs`` staying in their lane.

The taskiq worker (``lease_next(by_id=...)``) and the API's polling queue
(``lease_next()``) share this table. Only the worker path edits a Telegram
placeholder, so when the poller reclaimed a Telegram job whose lease had
expired it finished the summary in the API process and left the user's message
stuck on "Processing..." forever. Origin is only encoded at enqueue time
(``pending`` vs ``queued``) and both collapse to ``running``, so the poller
re-derives it from ``requests.bot_reply_message_id``.

These drive the real correlated-EXISTS query against Postgres (it cannot be
meaningfully exercised through a mocked session), so they need
``TEST_DATABASE_URL`` and skip cleanly without it -- see tests/conftest.py.
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
from tests.db_helpers_async import create_request

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.session import Database


async def _expired_running_job(
    session: AsyncSession,
    *,
    cid: str,
    bot_reply_message_id: int | None,
) -> int:
    """A request whose worker lease died mid-run, with or without a placeholder."""
    request_id = await create_request(
        session,
        type_="url",
        status="processing",
        correlation_id=cid,
        chat_id=42,
        user_id=1,
        input_url=f"https://example.com/{cid}",
    )
    if bot_reply_message_id is not None:
        await session.execute(
            update(Request)
            .where(Request.id == request_id)
            .values(bot_reply_message_id=bot_reply_message_id)
        )
    session.add(
        RequestProcessingJob(
            request_id=request_id,
            status="running",
            attempt_count=1,
            max_attempts=3,
            lease_owner=f"worker:taskiq:{request_id}",
            lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
            lease_token=1,
            correlation_id=cid,
        )
    )
    await session.flush()
    return request_id


@pytest.mark.asyncio
async def test_poller_skips_telegram_job_but_worker_can_still_take_it(
    database: Database, session: AsyncSession
) -> None:
    """The API poller leaves a placeholder-bearing job for the taskiq path."""
    request_id = await _expired_running_job(session, cid="cid-tg", bot_reply_message_id=1311)
    await session.commit()

    repo = RequestProcessingJobRepository(database)

    assert await repo.lease_next(lease_owner="api-host:abc", lease_ttl_seconds=300) is None

    leased = await repo.lease_next(
        lease_owner=f"worker:taskiq:{request_id}",
        lease_ttl_seconds=900,
        by_id=request_id,
    )
    assert leased is not None
    assert leased.request_id == request_id


@pytest.mark.asyncio
async def test_poller_still_reclaims_api_originated_job(
    database: Database, session: AsyncSession
) -> None:
    """No placeholder means no Telegram delivery to lose -- the poller may reclaim."""
    request_id = await _expired_running_job(session, cid="cid-api", bot_reply_message_id=None)
    await session.commit()

    repo = RequestProcessingJobRepository(database)
    leased = await repo.lease_next(lease_owner="api-host:abc", lease_ttl_seconds=300)

    assert leased is not None
    assert leased.request_id == request_id


@pytest.mark.asyncio
async def test_requeue_expired_leases_routes_each_row_to_its_owning_path(
    database: Database, session: AsyncSession
) -> None:
    """Telegram rows go back to ``pending`` (worker inbox), API rows to ``queued``."""
    tg_id = await _expired_running_job(session, cid="cid-tg2", bot_reply_message_id=1313)
    api_id = await _expired_running_job(session, cid="cid-api2", bot_reply_message_id=None)
    await session.commit()

    repo = RequestProcessingJobRepository(database)
    assert await repo.requeue_expired_leases() == 2

    async def _status(request_id: int) -> str | None:
        return await session.scalar(
            select(RequestProcessingJob.status).where(RequestProcessingJob.request_id == request_id)
        )

    assert await _status(tg_id) == "pending"
    assert await _status(api_id) == "queued"


@pytest.mark.asyncio
async def test_reclaim_frees_a_dead_workers_lease_but_not_the_pollers(
    database: Database, session: AsyncSession
) -> None:
    """Worker startup frees only leases a taskiq worker could have held."""
    worker_id = await _expired_running_job(session, cid="cid-dead", bot_reply_message_id=1317)
    api_id = await _expired_running_job(session, cid="cid-api3", bot_reply_message_id=None)
    # Re-stamp the API row with the poller's own owner format ({host}:{uuid}).
    await session.execute(
        update(RequestProcessingJob)
        .where(RequestProcessingJob.request_id == api_id)
        .values(lease_owner="somehost:0123456789abcdef")
    )
    await session.commit()

    repo = RequestProcessingJobRepository(database)
    assert await repo.reclaim_orphaned_worker_leases() == 1

    async def _row(request_id: int) -> RequestProcessingJob | None:
        return await session.scalar(
            select(RequestProcessingJob).where(RequestProcessingJob.request_id == request_id)
        )

    freed = await _row(worker_id)
    assert freed is not None
    assert freed.status == "pending"
    assert freed.lease_owner is None
    assert freed.last_error_code == "WORKER_RESTARTED"

    untouched = await _row(api_id)
    assert untouched is not None
    assert untouched.status == "running"
    assert untouched.lease_owner == "somehost:0123456789abcdef"

    # The interrupted run is not a failed run.
    request = await session.scalar(select(Request).where(Request.id == worker_id))
    assert request is not None
    assert request.status == "processing"


@pytest.mark.asyncio
async def test_release_lease_returns_job_without_failing_the_request(
    database: Database, session: AsyncSession
) -> None:
    """A cancelled worker hands the lease back; the request stays non-terminal."""
    request_id = await _expired_running_job(session, cid="cid-cancel", bot_reply_message_id=1315)
    await session.commit()

    repo = RequestProcessingJobRepository(database)
    lease_owner = f"worker:taskiq:{request_id}"
    leased = await repo.lease_next(lease_owner=lease_owner, lease_ttl_seconds=900, by_id=request_id)
    assert leased is not None

    assert await repo.release_lease(leased, lease_owner=lease_owner) is True

    await session.commit()
    job = await session.scalar(
        select(RequestProcessingJob).where(RequestProcessingJob.request_id == request_id)
    )
    assert job is not None
    assert job.status == "pending"
    assert job.lease_owner is None
    assert job.lease_expires_at is None

    request = await session.scalar(select(Request).where(Request.id == request_id))
    assert request is not None
    assert request.status == "processing"  # NOT "error" -- the run was cancelled, not failed
