"""High-level TranscriptionService.

Wraps the synchronous engines (ASR + diarization + decode) in ``asyncio``
primitives and gates concurrent first-load via an ``asyncio.Lock`` so the
sherpa-onnx recognizer is constructed exactly once even under burst load.

Threading model:

    * Cold path (first call): recognizer constructed under the lock, model
      files downloaded if missing.
    * Hot path: ``transcribe_media_path()`` runs the ffmpeg decode and the
      sherpa-onnx step in ``asyncio.to_thread``; diarization (when requested)
      runs in the same way on a separately-decoded 1.0x signal.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.logging_utils import get_logger

from .asr_engine import AsrEngine, OfflineAsrEngine, StreamingAsrEngine
from .audio_decoder import (
    AudioDecodeError,
    FfmpegNotInstalledError,
    NoAudioStreamError,
    decode_to_pcm,
    probe_duration_sec,
    require_ffmpeg,
)
from .diarization_engine import (
    DiarizationApiUnavailableError,
    diarize_pcm_sync,
    label_sentences,
)
from .model_resolver import (
    ensure_asr_model,
    ensure_diarization_models,
)
from .types import TranscriptionResult

if TYPE_CHECKING:
    from pathlib import Path

    from app.config.transcription import TranscriptionConfig

logger = get_logger(__name__)


class TranscriptionDisabledError(RuntimeError):
    """Raised when ``TranscriptionConfig.enabled`` is False but a caller requested transcription."""


class TranscriptionDurationExceededError(RuntimeError):
    """Raised when the input media exceeds ``TranscriptionConfig.max_duration_sec``."""

    def __init__(self, duration_sec: float, max_duration_sec: int) -> None:
        super().__init__(
            f"media duration {duration_sec:.0f}s exceeds the configured "
            f"max_duration_sec={max_duration_sec}",
        )
        self.duration_sec = duration_sec
        self.max_duration_sec = max_duration_sec


class TimestampsUnavailableError(RuntimeError):
    """Raised when diarization is requested but the ASR build lacks alignment data."""


@dataclass(frozen=True, slots=True)
class TranscribeOptions:
    """Per-call knobs that override ``TranscriptionConfig`` defaults."""

    with_diarization: bool | None = None
    speed: float | None = None
    num_speakers: int | None = None


class TranscriptionService:
    """Public entrypoint to the transcription adapter.

    A single instance is constructed at DI time, holds the lazy-loaded
    sherpa-onnx recognizer, and is safe to call concurrently.
    """

    def __init__(self, cfg: TranscriptionConfig) -> None:
        self._cfg = cfg
        self._asr_lock = asyncio.Lock()
        self._asr_engine: AsrEngine | None = None
        self._diarization_paths: tuple[Path, Path] | None = None
        self._diarization_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    @property
    def diarization_enabled(self) -> bool:
        return bool(self._cfg.diarization_enabled)

    async def transcribe_media_path(
        self,
        media_path: Path,
        *,
        options: TranscribeOptions | None = None,
        correlation_id: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe a local media file end-to-end.

        Raises ``TranscriptionDisabledError`` when the feature is gated off,
        ``TranscriptionDurationExceededError`` when the media is too long,
        ``FfmpegNotInstalledError`` / ``AudioDecodeError`` / ``NoAudioStreamError``
        when ffmpeg fails, and ``TimestampsUnavailableError`` when diarization
        was requested but the sherpa-onnx build does not return alignment data.
        """
        if not self._cfg.enabled:
            msg = "transcription is disabled (set TRANSCRIPTION_ENABLED=true to enable)"
            raise TranscriptionDisabledError(msg)

        opts = options or TranscribeOptions()
        speed = float(opts.speed if opts.speed is not None else self._cfg.speed)
        want_diarization = bool(
            opts.with_diarization
            if opts.with_diarization is not None
            else self._cfg.diarization_enabled,
        )
        num_speakers = int(
            opts.num_speakers if opts.num_speakers is not None else self._cfg.default_num_speakers,
        )

        duration = probe_duration_sec(media_path)
        if duration is not None and duration > self._cfg.max_duration_sec:
            raise TranscriptionDurationExceededError(duration, self._cfg.max_duration_sec)

        engine = await self._get_engine()

        sped_pcm = await asyncio.to_thread(decode_to_pcm, media_path, speed)
        plain_text, sentences = await asyncio.to_thread(
            engine.transcribe_sync,
            sped_pcm,
            speed=speed,
        )

        if not want_diarization:
            return TranscriptionResult(
                plain_text=plain_text,
                sentences=sentences or (),
                duration_sec=duration,
                used_diarization=False,
            )

        if sentences is None:
            msg = (
                "diarization needs sentence-level timestamps, but this sherpa-onnx "
                "build did not return alignment data. Upgrade sherpa-onnx or disable diarization."
            )
            raise TimestampsUnavailableError(msg)
        if not sentences:
            return TranscriptionResult(
                plain_text=plain_text,
                sentences=(),
                duration_sec=duration,
                used_diarization=True,
            )

        seg_onnx, emb_onnx = await self._ensure_diarization_models()
        if abs(speed - 1.0) < 1e-5:
            original_pcm = sped_pcm
        else:
            original_pcm = await asyncio.to_thread(decode_to_pcm, media_path, 1.0)
        turns = await asyncio.to_thread(
            diarize_pcm_sync,
            original_pcm,
            seg_onnx=seg_onnx,
            emb_onnx=emb_onnx,
            num_speakers=num_speakers,
            cluster_threshold=self._cfg.diarization_cluster_threshold,
            num_threads=self._cfg.num_threads,
        )
        labeled = label_sentences(sentences, turns)
        logger.info(
            "transcription_diarization_done",
            extra={
                "sentences": len(sentences),
                "turns": len(turns),
                "speakers_detected": len({turn.speaker for turn in turns}),
                "cid": correlation_id,
            },
        )
        # We attach per-sentence speaker labels back via a fresh tuple of
        # Sentence + parallel labels; expose them through speaker_turns so the
        # formatter can pick whichever representation it needs.
        labelled_sentences = tuple(sentence for _spk, sentence in labeled)
        return TranscriptionResult(
            plain_text=plain_text,
            sentences=labelled_sentences,
            speaker_turns=turns,
            duration_sec=duration,
            used_diarization=True,
        )

    async def warmup(self) -> None:
        """Pre-load the ASR recognizer outside the request path.

        Optional. Useful when running as a long-lived process where the first
        request's cold-start cost is unwelcome.
        """
        if not self._cfg.enabled:
            return
        require_ffmpeg()
        await self._get_engine()

    async def _get_engine(self) -> AsrEngine:
        if self._asr_engine is not None:
            return self._asr_engine
        async with self._asr_lock:
            if self._asr_engine is not None:
                return self._asr_engine
            model_path = await asyncio.to_thread(
                ensure_asr_model, self._cfg.model_path, self._cfg.language
            )
            backend = self._cfg.backend
            tokens_mode = self._cfg.tokens_mode
            if backend == "offline_transducer":
                self._asr_engine = OfflineAsrEngine(
                    model_dir=model_path,
                    num_threads=self._cfg.num_threads,
                    tokens_mode=tokens_mode,
                )
            else:
                self._asr_engine = StreamingAsrEngine(
                    model_dir=model_path,
                    num_threads=self._cfg.num_threads,
                    tokens_mode=tokens_mode,
                )
        return self._asr_engine

    async def _ensure_diarization_models(self) -> tuple[Path, Path]:
        if self._diarization_paths is not None:
            return self._diarization_paths
        async with self._diarization_lock:
            if self._diarization_paths is not None:
                return self._diarization_paths
            paths = await asyncio.to_thread(
                ensure_diarization_models,
                segmentation_key=self._cfg.diarization_model,
                embedding_model_filename=self._cfg.embedding_model_filename,
                cache_dir=self._cfg.diarization_model_path,
            )
            self._diarization_paths = paths
        return self._diarization_paths


__all__ = [
    "AudioDecodeError",
    "DiarizationApiUnavailableError",
    "FfmpegNotInstalledError",
    "NoAudioStreamError",
    "TimestampsUnavailableError",
    "TranscribeOptions",
    "TranscriptionDisabledError",
    "TranscriptionDurationExceededError",
    "TranscriptionService",
]
