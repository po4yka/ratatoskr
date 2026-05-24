"""CPU-only audio/video transcription adapter (sherpa-onnx + ffmpeg).

Public surface:

    TranscriptionService        -- DI-injectable async entrypoint
    TranscribeOptions           -- per-call overrides
    TranscriptionResult         -- output dataclass
    Sentence, SpeakerTurn       -- nested output dataclasses

All ``*Error`` classes are exported so callers can attribute failures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .audio_decoder import (
    AudioDecodeError,
    FfmpegNotInstalledError,
    NoAudioStreamError,
)
from .diarization_engine import DiarizationApiUnavailableError
from .media_fetcher import MediaFetchError, fetch_url_to_local_sync
from .model_resolver import ModelDirectoryError, ModelDownloadError
from .sentence_grouper import format_mmss
from app.application.ports.transcriptions import TranscribeOptions
from .service import (
    TimestampsUnavailableError,
    TranscriptionDisabledError,
    TranscriptionDurationExceededError,
    TranscriptionService,
)
from .types import Sentence, SpeakerTurn, TranscriptionResult

if TYPE_CHECKING:
    from app.config.transcription import TranscriptionConfig


_SERVICE_CACHE: dict[int, TranscriptionService] = {}


def get_or_create_transcription_service(cfg: TranscriptionConfig) -> TranscriptionService:
    """Return a process-wide TranscriptionService keyed on the config identity.

    The sherpa-onnx recognizer is heavy (~80 MB ONNX) and we don't want
    parallel construction sites (the /transcribe handler, the voice processor,
    the URL-pipeline auto-fill) to each spin up their own copy. This shared
    accessor caches the service by ``id(cfg)`` so all callers that resolve the
    same ``cfg.transcription`` get the same instance.
    """
    key = id(cfg)
    cached = _SERVICE_CACHE.get(key)
    if cached is not None:
        return cached
    service = TranscriptionService(cfg)
    _SERVICE_CACHE[key] = service
    return service


def _reset_transcription_service_cache() -> None:
    """Test hook: clear the cache so a fresh config produces a fresh service."""
    _SERVICE_CACHE.clear()


__all__ = [
    "AudioDecodeError",
    "DiarizationApiUnavailableError",
    "FfmpegNotInstalledError",
    "MediaFetchError",
    "ModelDirectoryError",
    "ModelDownloadError",
    "NoAudioStreamError",
    "Sentence",
    "SpeakerTurn",
    "TimestampsUnavailableError",
    "TranscribeOptions",
    "TranscriptionDisabledError",
    "TranscriptionDurationExceededError",
    "TranscriptionResult",
    "TranscriptionService",
    "fetch_url_to_local_sync",
    "format_mmss",
    "get_or_create_transcription_service",
]
