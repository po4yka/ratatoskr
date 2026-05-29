"""Transcription service port for the application layer.

Application services that need to perform transcription depend on this
Protocol instead of the concrete ``app.adapters.transcription.TranscriptionService``.
The adapter satisfies this protocol structurally (no explicit ``implements``
declaration needed).

The return type of ``transcribe_media_path`` is intentionally ``Any`` here to
avoid importing ``TranscriptionResult`` from ``app.adapters.transcription.types``,
which would violate the application→adapters layering rule.  Callers (i.e.
``TranscriptionJobService``) access only well-known fields on the result and are
therefore safe without a tighter return-type annotation at the port level.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from app.application.ports.transcriptions import TranscribeOptions


@runtime_checkable
class TranscriptionServicePort(Protocol):
    """Minimal interface required by TranscriptionJobService.

    Only the single method consumed by the job service is included;
    other methods on the concrete service are adapter-internal.
    """

    async def transcribe_media_path(
        self,
        media_path: Path,
        *,
        options: TranscribeOptions | None = None,
        correlation_id: str | None = None,
        progress_callback: Callable[[str, str, float, str], Awaitable[None] | None] | None = None,
    ) -> Any:
        """Transcribe a local media file end-to-end.

        Returns a TranscriptionResult-compatible object with at least:
        ``plain_text``, ``detected_language``, ``duration_sec``,
        ``sentences``, ``speaker_turns``, and ``used_diarization`` attributes.
        """
        ...


__all__ = ["TranscriptionServicePort"]
