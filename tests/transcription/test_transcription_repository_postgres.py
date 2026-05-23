from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.application.ports.transcriptions import (
    TranscriptionArtifactCreate,
    TranscriptionJobCreate,
    TranscriptionRepositoryPort,
)
from app.db.models import User
from app.infrastructure.persistence.repositories.transcription_repository import (
    TranscriptionRepositoryAdapter,
)

if TYPE_CHECKING:
    from app.db.session import Database


async def _create_user(database: Database, user_id: int = 9801) -> int:
    async with database.transaction() as session:
        session.add(User(telegram_user_id=user_id, username=f"transcription-{user_id}"))
    return user_id


@pytest.mark.asyncio
async def test_transcription_repository_persists_job_and_artifact(database: Database) -> None:
    user_id = await _create_user(database)
    repo = TranscriptionRepositoryAdapter(database)
    assert isinstance(repo, TranscriptionRepositoryPort)

    job = await repo.create_job(
        TranscriptionJobCreate(
            user_id=user_id,
            source_type="telegram_voice",
            telegram_chat_id=123,
            telegram_message_id=456,
            language="en",
            backend="streaming",
            tokens_mode="bpe",
            model_identifier="en:streaming:bpe:transcription",
            status="started",
            audio_hash="a" * 64,
            correlation_id="cid-transcription",
            metadata_json={"local_media_path": "/tmp/secret.ogg", "diarization_enabled": True},
        )
    )

    artifact = await repo.complete_job_with_artifact(
        job.id,
        TranscriptionArtifactCreate(
            job_id=job.id,
            user_id=user_id,
            source_type="telegram_voice",
            telegram_chat_id=123,
            telegram_message_id=456,
            language="en",
            backend="streaming",
            tokens_mode="bpe",
            model_identifier="en:streaming:bpe:transcription",
            status="completed",
            duration_sec=12.5,
            plain_text="hello world",
            sentences_json=[{"start_sec": 0.0, "text": "hello world"}],
            speaker_turns_json=[{"start": 0.0, "end": 12.5, "speaker": 0, "label": "SPEAKER_00"}],
            audio_hash=job.audio_hash,
            correlation_id="cid-transcription",
            metadata_json={"used_diarization": True, "authorization": "Bearer raw"},
        ),
    )

    assert artifact.job_id == job.id
    assert artifact.user_id == user_id
    assert artifact.source_type == "telegram_voice"
    assert artifact.plain_text == "hello world"
    assert artifact.sentences_json == [{"start_sec": 0.0, "text": "hello world"}]
    assert artifact.speaker_turns_json == [
        {"start": 0.0, "end": 12.5, "speaker": 0, "label": "SPEAKER_00"}
    ]
    assert artifact.audio_hash == "a" * 64
    assert artifact.metadata_json == {"used_diarization": True}

    listed = await repo.list_artifacts_for_user(user_id)
    assert [item.id for item in listed] == [artifact.id]


@pytest.mark.asyncio
async def test_transcription_repository_marks_jobs_failed(database: Database) -> None:
    user_id = await _create_user(database, user_id=9802)
    repo = TranscriptionRepositoryAdapter(database)
    job = await repo.create_job(
        TranscriptionJobCreate(user_id=user_id, source_type="url", correlation_id="cid-failed")
    )

    failed = await repo.fail_job(
        job.id,
        error_code="duration_exceeded",
        error_message="media duration exceeded",
    )

    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_code == "duration_exceeded"
    assert failed.error_message == "media duration exceeded"
