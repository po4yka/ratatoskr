"""Postgres-backed tests for the audio generation repository."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.db.models import AudioGeneration, Request, Summary
from app.db.session import Database
from app.infrastructure.persistence.repositories.audio_generation_repository import (
    AudioGenerationRepositoryAdapter,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture
async def database() -> AsyncGenerator[Database]:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres repository tests")

    db = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    await db.migrate()
    await _clear(db)
    try:
        yield db
    finally:
        await _clear(db)
        await db.dispose()


async def _clear(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(delete(AudioGeneration))
        await session.execute(delete(Summary))
        await session.execute(delete(Request))


async def _summary(database: Database, *, suffix: str) -> Summary:
    async with database.transaction() as session:
        request = Request(
            type="url",
            status="completed",
            correlation_id=f"audio-{suffix}",
            user_id=13001,
            input_url=f"https://example.com/audio/{suffix}",
            normalized_url=f"https://example.com/audio/{suffix}",
            dedupe_hash=f"audio-{suffix}",
        )
        session.add(request)
        await session.flush()
        summary = Summary(request_id=request.id, lang="en", json_payload={"summary_250": suffix})
        session.add(summary)
        await session.flush()
        return summary


@pytest.mark.asyncio
async def test_audio_generation_repository_start_complete_and_read(
    database: Database,
) -> None:
    repo = AudioGenerationRepositoryAdapter(database)
    summary = await _summary(database, suffix="completed")

    await repo.async_mark_generation_started(
        summary_id=summary.id,
        source_field="summary_1000",
        voice_id="voice-a",
        model_name="model-a",
        language="en",
        char_count=100,
    )
    started = await repo.async_get_latest_generation(summary.id)
    assert started is not None
    assert started["status"] == "generating"
    assert started["voice_id"] == "voice-a"

    await repo.async_mark_generation_completed(
        summary_id=summary.id,
        source_field="summary_1000",
        file_path="/tmp/audio.mp3",
        file_size_bytes=123,
        char_count=100,
        latency_ms=456,
    )
    completed = await repo.async_get_completed_generation(summary.id, "summary_1000")
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["file_path"] == "/tmp/audio.mp3"
    assert completed["file_size_bytes"] == 123

    matching = await repo.async_get_completed_generation(
        summary.id,
        "summary_1000",
        voice_id="voice-a",
        model_name="model-a",
    )
    mismatched = await repo.async_get_completed_generation(
        summary.id,
        "summary_1000",
        voice_id="voice-b",
        model_name="model-a",
    )
    assert matching is not None
    assert mismatched is None


@pytest.mark.asyncio
async def test_audio_generation_repository_failed_upserts(database: Database) -> None:
    repo = AudioGenerationRepositoryAdapter(database)
    summary = await _summary(database, suffix="failed")

    await repo.async_mark_generation_failed(
        summary_id=summary.id,
        source_field="tldr",
        error_text="first",
        latency_ms=1,
    )
    await repo.async_mark_generation_failed(
        summary_id=summary.id,
        source_field="summary_250",
        error_text="second",
        latency_ms=2,
    )
    latest = await repo.async_get_latest_generation(summary.id)
    assert latest is not None
    assert latest["status"] == "error"
    assert latest["source_field"] == "summary_250"
    assert latest["error_text"] == "second"
    assert latest["latency_ms"] == 2
