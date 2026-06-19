"""Application use case for audio generation from summaries."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from app.application.dto.audio_generation import AudioGenerationResult
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.application.ports.audio import (
        AudioGenerationRepositoryPort,
        AudioStoragePort,
        TTSProviderPort,
    )
    from app.application.ports.summaries import SummaryRepositoryPort

logger = get_logger(__name__)

_VALID_SOURCE_FIELDS = frozenset({"summary_250", "summary_1000", "tldr"})


class TTSService:
    """Generate and cache audio for summary content via application ports."""

    def __init__(
        self,
        *,
        summary_repository: SummaryRepositoryPort,
        audio_generation_repository: AudioGenerationRepositoryPort,
        tts_provider: TTSProviderPort,
        audio_storage: AudioStoragePort,
        voice_id: str,
        model_name: str,
        max_chars_per_request: int,
    ) -> None:
        self._summary_repo = summary_repository
        self._audio_repo = audio_generation_repository
        self._tts_provider = tts_provider
        self._audio_storage = audio_storage
        self._voice_id = voice_id
        self._model_name = model_name
        self._max_chars_per_request = max_chars_per_request

    async def generate_audio(
        self,
        summary_id: int,
        *,
        source_field: str = "summary_1000",
    ) -> AudioGenerationResult:
        """Generate audio for a summary, returning cached output when available."""
        if source_field not in _VALID_SOURCE_FIELDS:
            source_field = "summary_1000"

        existing = await self._audio_repo.async_get_completed_generation(
            summary_id,
            source_field,
            voice_id=self._voice_id,
            model_name=self._model_name,
        )
        if existing and existing.get("file_path"):
            return AudioGenerationResult(
                summary_id=summary_id,
                status="completed",
                file_path=existing.get("file_path"),
                file_size_bytes=existing.get("file_size_bytes"),
                char_count=existing.get("char_count"),
                latency_ms=existing.get("latency_ms"),
            )

        summary = await self._summary_repo.async_get_summary_by_id(summary_id)
        if summary is None:
            return AudioGenerationResult(
                summary_id=summary_id,
                status="error",
                error="Summary not found",
            )

        payload = summary.get("json_payload") or {}
        text, source_field = self._resolve_source_text(payload, source_field)
        if not text:
            return AudioGenerationResult(
                summary_id=summary_id,
                status="error",
                error="No summary text available",
            )

        char_count = len(text)
        await self._audio_repo.async_mark_generation_started(
            summary_id=summary_id,
            source_field=source_field,
            voice_id=self._voice_id,
            model_name=self._model_name,
            language=summary.get("lang"),
            char_count=char_count,
        )

        started_at = time.monotonic()
        try:
            audio_bytes = await self._tts_provider.synthesize(
                text,
                use_long_form=char_count > self._max_chars_per_request,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - started_at) * 1000)
            error_text = str(exc)[:500]
            await self._audio_repo.async_mark_generation_failed(
                summary_id=summary_id,
                source_field=source_field,
                error_text=error_text,
                latency_ms=latency_ms,
            )
            logger.error(
                "tts_generation_failed",
                extra={"summary_id": summary_id, "error": error_text, "latency_ms": latency_ms},
            )
            return AudioGenerationResult(
                summary_id=summary_id,
                status="error",
                error=error_text,
                latency_ms=latency_ms,
            )

        latency_ms = int((time.monotonic() - started_at) * 1000)
        stored_file = await self._audio_storage.save_audio(summary_id, audio_bytes)
        await self._audio_repo.async_mark_generation_completed(
            summary_id=summary_id,
            source_field=source_field,
            file_path=stored_file.file_path,
            file_size_bytes=stored_file.file_size_bytes,
            char_count=char_count,
            latency_ms=latency_ms,
        )
        logger.info(
            "tts_generation_completed",
            extra={
                "summary_id": summary_id,
                "char_count": char_count,
                "file_size_bytes": stored_file.file_size_bytes,
                "latency_ms": latency_ms,
                "source_field": source_field,
            },
        )
        return AudioGenerationResult(
            summary_id=summary_id,
            status="completed",
            file_path=stored_file.file_path,
            file_size_bytes=stored_file.file_size_bytes,
            char_count=char_count,
            latency_ms=latency_ms,
        )

    async def get_audio_status(self, summary_id: int) -> AudioGenerationResult | None:
        """Return the latest persisted audio generation state for a summary."""
        generation = await self._audio_repo.async_get_latest_generation(summary_id)
        if generation is None:
            return None
        return AudioGenerationResult(
            summary_id=summary_id,
            status=generation.get("status", "pending"),
            file_path=generation.get("file_path"),
            file_size_bytes=generation.get("file_size_bytes"),
            char_count=generation.get("char_count"),
            latency_ms=generation.get("latency_ms"),
            error=generation.get("error_text"),
        )

    async def close(self) -> None:
        await self._tts_provider.close()

    @staticmethod
    def _resolve_source_text(payload: dict[str, Any], source_field: str) -> tuple[str, str]:
        text = str(payload.get(source_field, "") or "").strip()
        if text:
            return text, source_field

        for fallback in ("summary_1000", "summary_250", "tldr"):
            text = str(payload.get(fallback, "") or "").strip()
            if text:
                return text, fallback
        return "", source_field
