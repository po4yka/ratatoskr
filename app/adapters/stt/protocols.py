"""Protocols for remote speech-to-text providers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from app.adapters.transcription.types import TranscriptionResult


@runtime_checkable
class STTClientProtocol(Protocol):
    """Async speech-to-text client for a local media file."""

    async def transcribe_file(
        self,
        media_path: Path,
        *,
        language: str | None = None,
        correlation_id: str | None = None,
    ) -> TranscriptionResult:
        """Return a transcript for ``media_path``."""
