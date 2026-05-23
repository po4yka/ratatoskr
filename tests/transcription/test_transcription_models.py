from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from app.db.models import (
    ALL_MODELS,
    TranscriptionArtifact,
    TranscriptionJob,
    TranscriptionProgressEvent,
)


def test_transcription_models_are_registered() -> None:
    assert TranscriptionJob in ALL_MODELS
    assert TranscriptionArtifact in ALL_MODELS
    assert TranscriptionProgressEvent in ALL_MODELS


def test_transcription_tables_include_required_contract_columns() -> None:
    job_columns = TranscriptionJob.__table__.columns
    artifact_columns = TranscriptionArtifact.__table__.columns

    shared_columns = (
        "user_id",
        "request_id",
        "telegram_chat_id",
        "telegram_message_id",
        "source_type",
        "language",
        "backend",
        "tokens_mode",
        "model_identifier",
        "status",
        "duration_sec",
        "audio_hash",
        "correlation_id",
    )
    for name in shared_columns:
        assert name in job_columns
        assert name in artifact_columns

    for name in (
        "source_url",
        "idempotency_key",
        "current_stage",
        "progress",
        "attempt_count",
        "max_attempts",
        "lease_owner",
        "lease_expires_at",
        "retry_after",
        "queued_at",
        "started_at",
        "completed_at",
    ):
        assert name in job_columns

    for name in ("plain_text", "sentences_json", "speaker_turns_json"):
        assert name in artifact_columns
    for name in ("event_id", "job_id", "sequence", "stage", "status", "payload"):
        assert name in TranscriptionProgressEvent.__table__.columns

    assert isinstance(artifact_columns["plain_text"].type, sa.Text)
    assert "raw_audio" not in job_columns
    assert "raw_audio" not in artifact_columns
    assert "local_media_path" not in job_columns
    assert "local_media_path" not in artifact_columns


def test_transcription_migration_creates_jobs_and_artifacts_without_raw_audio() -> None:
    migration = (
        Path(__file__).parents[2] / "app/db/alembic/versions/0024_add_transcription_artifacts.py"
    ).read_text()
    queue_migration = (
        Path(__file__).parents[2]
        / "app/db/alembic/versions/0025_add_transcription_job_queue_state.py"
    ).read_text()

    assert 'op.create_table(\n        "transcription_jobs"' in migration
    assert 'op.create_table(\n        "transcription_artifacts"' in migration
    for name in (
        "user_id",
        "request_id",
        "telegram_chat_id",
        "telegram_message_id",
        "source_url",
        "idempotency_key",
        "source_type",
        "language",
        "backend",
        "tokens_mode",
        "model_identifier",
        "status",
        "duration_sec",
        "plain_text",
        "sentences_json",
        "speaker_turns_json",
        "audio_hash",
        "correlation_id",
    ):
        assert f'"{name}"' in migration or f'"{name}"' in queue_migration
    assert 'op.create_table(\n        "transcription_progress_events"' in queue_migration
    assert "raw_audio" not in migration
    assert "raw_audio" not in queue_migration
    assert "local_media_path" not in migration
    assert "local_media_path" not in queue_migration
