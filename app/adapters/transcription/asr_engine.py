"""sherpa-onnx streaming ASR wrapper.

Encapsulates the lazy load of ``sherpa_onnx.OnlineRecognizer`` plus the
best-effort token/timestamp extraction across sherpa-onnx Python wrapper
versions. The synchronous ``transcribe_sync`` is meant to be wrapped in
``asyncio.to_thread`` by callers.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import numpy as np

from app.core.logging_utils import get_logger

from .audio_decoder import SAMPLE_RATE
from .model_resolver import find_model_file
from .sentence_grouper import group_into_sentences

if TYPE_CHECKING:
    from pathlib import Path

    from .types import Sentence

logger = get_logger(__name__)

_FEATURE_DIM = 80
_TAIL_PAD_SEC = 0.66  # mirrors yapsnap; flushes the final streaming chunk


class _TimestampsUnavailableMarker:
    """Sentinel used to signal "build does not expose alignment data"."""


_TIMESTAMPS_UNAVAILABLE = _TimestampsUnavailableMarker()


class StreamingAsrEngine:
    """Lazy-loaded sherpa-onnx streaming Zipformer recognizer.

    A single instance is reused across requests; the underlying recognizer is
    constructed on the first call and held for the lifetime of the engine.
    """

    def __init__(self, *, model_dir: Path, num_threads: int) -> None:
        self._model_dir = model_dir
        self._num_threads = max(1, int(num_threads or 1))
        self._recognizer: Any | None = None

    def _ensure_recognizer(self) -> Any:
        if self._recognizer is not None:
            return self._recognizer
        import sherpa_onnx

        encoder = find_model_file(self._model_dir, "encoder")
        decoder = find_model_file(self._model_dir, "decoder")
        joiner = find_model_file(self._model_dir, "joiner")
        tokens = self._model_dir / "tokens.txt"

        logger.info(
            "transcription_asr_recognizer_load",
            extra={
                "model_dir": str(self._model_dir),
                "encoder": encoder.name,
                "num_threads": self._num_threads,
            },
        )
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            encoder=str(encoder),
            decoder=str(decoder),
            joiner=str(joiner),
            tokens=str(tokens),
            num_threads=self._num_threads or os.cpu_count() or 1,
            sample_rate=SAMPLE_RATE,
            feature_dim=_FEATURE_DIM,
            decoding_method="greedy_search",
            provider="cpu",
            enable_endpoint_detection=False,
        )
        return self._recognizer

    def transcribe_sync(
        self,
        samples: np.ndarray,
        *,
        speed: float,
    ) -> tuple[str, tuple[Sentence, ...] | None]:
        """Decode ``samples`` and return (plain_text, sentences or None).

        ``sentences`` is ``None`` when the installed sherpa-onnx build does not
        expose alignment data; callers that need timestamps must treat this as
        a hard error.
        """
        if len(samples) == 0:
            return "", ()

        recognizer = self._ensure_recognizer()
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)

        tail_len = int(_TAIL_PAD_SEC * SAMPLE_RATE)
        if tail_len > 0:
            stream.accept_waveform(SAMPLE_RATE, np.zeros(tail_len, dtype=np.float32))
        stream.input_finished()

        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)

        text_result = recognizer.get_result(stream)
        text = (
            text_result if isinstance(text_result, str) else getattr(text_result, "text", "") or ""
        ).strip()

        tokens, times = _try_extract_timestamps(recognizer, stream, text_result)
        if not tokens or not times or len(tokens) != len(times):
            return text, None

        sentences = group_into_sentences(tokens, [float(t) for t in times], speed)
        return text, sentences


def _try_extract_timestamps(
    recognizer: Any,
    stream: Any,
    text_result: Any,
) -> tuple[list[str], list[float]]:
    """Best-effort token/timestamp extraction across sherpa-onnx wrapper versions.

    Returns ``([], [])`` when nothing usable is exposed. Mirrors yapsnap's
    triple-fallback logic so that newer and older sherpa-onnx Python wheels
    both work without code changes.
    """
    json_method = getattr(recognizer, "get_result_as_json_string", None)
    if callable(json_method):
        try:
            parsed = json.loads(json_method(stream))
            tokens = list(parsed.get("tokens") or [])
            times = list(parsed.get("timestamps") or [])
            if tokens and times:
                return tokens, times
        except Exception as exc:
            logger.debug("transcription_timestamp_path_json_failed", extra={"error": str(exc)})

    stream_result = getattr(stream, "result", None)
    if stream_result is not None:
        tokens = list(getattr(stream_result, "tokens", None) or [])
        times = list(getattr(stream_result, "timestamps", None) or [])
        if tokens and times:
            return tokens, times

    if not isinstance(text_result, str) and text_result is not None:
        tokens = list(getattr(text_result, "tokens", None) or [])
        times = list(getattr(text_result, "timestamps", None) or [])
        if tokens and times:
            return tokens, times

    return [], []
