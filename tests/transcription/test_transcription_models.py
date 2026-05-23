from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from app.db.models import ALL_MODELS, TranscriptionArtifact, TranscriptionJob


def test_transcription_models_are_registered() -> None:
    assert TranscriptionJob in ALL_MODELS
    assert TranscriptionArtifact in ALL_MODELS


def test_transcription_tables_include_required_contract_columns() -> None:
    job_columns = TranscriptionJob.__table__.columns
    artifact_columns = TranscriptionArtifact.__table__.columns

    for name in (
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
    ):
        assert name in job_columns
        assert name in artifact_columns

    for name in ("plain_text", "sentences_json", "speaker_turns_json"):
        assert name in artifact_columns

    assert isinstance(artifact_columns["plain_text"].type, sa.Text)
    assert "raw_audio" not in job_columns
    assert "raw_audio" not in artifact_columns
    assert "local_media_path" not in job_columns
    assert "local_media_path" not in artifact_columns


def test_transcription_migration_creates_jobs_and_artifacts_without_raw_audio() -> None:
    migration = (
        Path(__file__).parents[2] / "app/db/alembic/versions/0024_add_transcription_artifacts.py"
    ).read_text()

    assert 'op.create_table(\n        "transcription_jobs"' in migration
    assert 'op.create_table(\n        "transcription_artifacts"' in migration
    for name in (
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
        "plain_text",
        "sentences_json",
        "speaker_turns_json",
        "audio_hash",
        "correlation_id",
    ):
        assert f'"{name}"' in migration
    assert "raw_audio" not in migration
    assert "local_media_path" not in migration
