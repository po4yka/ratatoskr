"""SQLAlchemy adapter for cached TTS audio generation records."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import AudioGeneration, model_to_dict

if TYPE_CHECKING:
    from app.db.session import Database


class AudioGenerationRepositoryAdapter:
    """Persist and query generated summary audio."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_get_completed_generation(
        self,
        summary_id: int,
        source_field: str,
        *,
        voice_id: str | None = None,
        model_name: str | None = None,
    ) -> dict[str, Any] | None:
        """Return a completed generation record for the requested source field."""
        conditions = [
            AudioGeneration.summary_id == summary_id,
            AudioGeneration.source_field == source_field,
            AudioGeneration.status == "completed",
        ]
        if voice_id is not None:
            conditions.append(AudioGeneration.voice_id == voice_id)
        if model_name is not None:
            conditions.append(AudioGeneration.model == model_name)
        async with self._database.session() as session:
            row = await session.scalar(select(AudioGeneration).where(*conditions))
            return model_to_dict(row)

    async def async_get_latest_generation(self, summary_id: int) -> dict[str, Any] | None:
        """Return the latest generation row for the summary."""
        async with self._database.session() as session:
            row = await session.scalar(
                select(AudioGeneration)
                .where(AudioGeneration.summary_id == summary_id)
                .order_by(AudioGeneration.created_at.desc())
                .limit(1)
            )
            return model_to_dict(row)

    async def async_mark_generation_started(
        self,
        *,
        summary_id: int,
        source_field: str,
        voice_id: str,
        model_name: str,
        language: str | None,
        char_count: int,
    ) -> None:
        """Create or update a generation row in generating state."""
        async with self._database.transaction() as session:
            stmt = (
                insert(AudioGeneration)
                .values(
                    summary_id=summary_id,
                    provider="elevenlabs",
                    voice_id=voice_id,
                    model=model_name,
                    source_field=source_field,
                    language=language,
                    status="generating",
                    char_count=char_count,
                    error_text=None,
                    file_path=None,
                    file_size_bytes=None,
                    latency_ms=None,
                )
                .on_conflict_do_update(
                    index_elements=[AudioGeneration.summary_id],
                    set_={
                        "voice_id": voice_id,
                        "model": model_name,
                        "source_field": source_field,
                        "language": language,
                        "status": "generating",
                        "char_count": char_count,
                        "error_text": None,
                        "file_path": None,
                        "file_size_bytes": None,
                        "latency_ms": None,
                    },
                )
            )
            await session.execute(stmt)

    async def async_mark_generation_completed(
        self,
        *,
        summary_id: int,
        source_field: str,
        file_path: str,
        file_size_bytes: int,
        char_count: int,
        latency_ms: int,
    ) -> None:
        """Persist a completed generation result."""
        async with self._database.transaction() as session:
            await session.execute(
                update(AudioGeneration)
                .where(AudioGeneration.summary_id == summary_id)
                .values(
                    source_field=source_field,
                    status="completed",
                    file_path=file_path,
                    file_size_bytes=file_size_bytes,
                    char_count=char_count,
                    latency_ms=latency_ms,
                    error_text=None,
                )
            )

    async def async_mark_generation_failed(
        self,
        *,
        summary_id: int,
        source_field: str,
        error_text: str,
        latency_ms: int,
    ) -> None:
        """Persist a failed generation result."""
        async with self._database.transaction() as session:
            stmt = (
                insert(AudioGeneration)
                .values(
                    summary_id=summary_id,
                    provider="elevenlabs",
                    voice_id="",
                    model="",
                    source_field=source_field,
                    status="error",
                    error_text=error_text,
                    latency_ms=latency_ms,
                )
                .on_conflict_do_update(
                    index_elements=[AudioGeneration.summary_id],
                    set_={
                        "source_field": source_field,
                        "status": "error",
                        "error_text": error_text,
                        "latency_ms": latency_ms,
                    },
                )
            )
            await session.execute(stmt)
