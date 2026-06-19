"""Audio synthesis and storage ports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.application.dto.audio_generation import StoredAudioFileDTO


@runtime_checkable
class TTSProviderPort(Protocol):
    async def synthesize(self, text: str, *, use_long_form: bool = False) -> bytes:
        """Synthesize speech for the provided text."""

    async def close(self) -> None:
        """Release provider resources."""


@runtime_checkable
class AudioStoragePort(Protocol):
    async def save_audio(self, summary_id: int, audio_bytes: bytes) -> StoredAudioFileDTO:
        """Persist synthesized audio and return its storage metadata."""


@runtime_checkable
class AudioGenerationRepositoryPort(Protocol):
    async def async_get_completed_generation(
        self,
        summary_id: int,
        source_field: str,
        *,
        voice_id: str | None = None,
        model_name: str | None = None,
    ) -> dict[str, Any] | None:
        """Return a completed generation for the summary/source pair."""

    async def async_get_latest_generation(self, summary_id: int) -> dict[str, Any] | None:
        """Return the latest generation row for a summary."""

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

    async def async_mark_generation_failed(
        self,
        *,
        summary_id: int,
        source_field: str,
        error_text: str,
        latency_ms: int,
    ) -> None:
        """Persist a failed generation result."""
