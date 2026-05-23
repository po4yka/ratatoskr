from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.application.ports.transcriptions import TranscriptionJobCreate
from app.db.models import User
from app.db.models.transcription import TranscriptionArtifact, TranscriptionJob
from app.infrastructure.persistence.repositories.transcription_repository import (
    TranscriptionRepositoryAdapter,
)

if TYPE_CHECKING:
    from app.db.session import Database


async def _create_user(database: Database, user_id: int = 9901) -> int:
    async with database.transaction() as session:
        await session.execute(delete(TranscriptionArtifact))
        await session.execute(delete(TranscriptionJob))
        await session.execute(delete(User).where(User.telegram_user_id == user_id))
        session.add(User(telegram_user_id=user_id, username=f"transcription-queue-{user_id}"))
    return user_id


@pytest.mark.asyncio
async def test_duplicate_telegram_job_reuses_idempotency_key(database: Database) -> None:
    user_id = await _create_user(database)
    repo = TranscriptionRepositoryAdapter(database)
    first = await repo.enqueue_job(
        TranscriptionJobCreate(
            user_id=user_id,
            source_type="telegram_voice",
            telegram_chat_id=123,
            telegram_message_id=456,
            idempotency_key=f"telegram:{user_id}:123:456",
            correlation_id="cid-one",
        )
    )
    second = await repo.enqueue_job(
        TranscriptionJobCreate(
            user_id=user_id,
            source_type="telegram_voice",
            telegram_chat_id=123,
            telegram_message_id=456,
            idempotency_key=f"telegram:{user_id}:123:456",
            correlation_id="cid-two",
        )
    )

    assert second.id == first.id
    assert second.status == "queued"


@pytest.mark.asyncio
async def test_audio_hash_job_reuses_idempotency_key(database: Database) -> None:
    user_id = await _create_user(database, user_id=9904)
    repo = TranscriptionRepositoryAdapter(database)
    first = await repo.enqueue_job(
        TranscriptionJobCreate(
            user_id=user_id,
            source_type="local_media",
            audio_hash="a" * 64,
            idempotency_key=f"audio:{'a' * 64}",
        )
    )
    second = await repo.enqueue_job(
        TranscriptionJobCreate(
            user_id=user_id,
            source_type="local_media",
            audio_hash="a" * 64,
            idempotency_key=f"audio:{'a' * 64}",
        )
    )

    assert second.id == first.id


@pytest.mark.asyncio
async def test_progress_events_are_replayable_in_sequence(database: Database) -> None:
    user_id = await _create_user(database, user_id=9902)
    repo = TranscriptionRepositoryAdapter(database)
    job = await repo.enqueue_job(
        TranscriptionJobCreate(
            user_id=user_id,
            source_type="url",
            source_url="https://example.com/audio.mp3?token=raw",
            idempotency_key="url:9902:https://example.com/audio.mp3",
            correlation_id="cid-progress",
        )
    )
    await repo.append_progress_event(
        job_id=job.id,
        stage="queued",
        status="queued",
        message="Queued",
        progress=0.0,
        payload={"authorization": "Bearer raw", "safe": True},
        correlation_id="cid-progress",
    )
    await repo.append_progress_event(
        job_id=job.id,
        stage="downloading_media",
        status="running",
        message="Downloading",
        progress=0.1,
        payload={"safe": True},
        correlation_id="cid-progress",
    )
    await repo.append_progress_event(
        job_id=job.id,
        stage="done",
        status="done",
        message="Done",
        progress=1.0,
        payload=None,
        correlation_id="cid-progress",
    )

    events = await repo.list_progress_events(job.id)

    assert [event.sequence for event in events] == [1, 2, 3]
    assert [event.stage for event in events] == ["queued", "downloading_media", "done"]
    assert events[0].payload == {"safe": True}


@pytest.mark.asyncio
async def test_expired_running_job_is_requeued_for_retry(database: Database) -> None:
    user_id = await _create_user(database, user_id=9903)
    repo = TranscriptionRepositoryAdapter(database)
    job = await repo.enqueue_job(
        TranscriptionJobCreate(
            user_id=user_id,
            source_type="url",
            source_url="https://example.com/audio.mp3",
            idempotency_key="url:9903:https://example.com/audio.mp3",
            correlation_id="cid-retry",
            max_attempts=2,
        )
    )
    leased = await repo.lease_next(lease_owner="worker-one", lease_ttl_seconds=-1)

    assert leased is not None
    assert leased.id == job.id
    assert await repo.requeue_expired_leases() == 1
    leased_again = await repo.lease_next(lease_owner="worker-two", lease_ttl_seconds=30)

    assert leased_again is not None
    assert leased_again.id == job.id
    assert leased_again.attempt_count == 2
