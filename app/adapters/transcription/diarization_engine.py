"""ONNX-based speaker diarization ("who spoke when") via sherpa-onnx.

Ported from yapsnap/diarize.py. Pipeline (all in the ONNX runtime, no torch):

    16kHz mono float32 PCM
        |
        v
    [segmentation]  pyannote-3.0 (default) or reverb-v1
        |                neural speaker-activity map -> turn boundaries
        v
    [embedding]     3D-Speaker CAM++ -> per-segment voiceprints
        |
        v
    [FastClustering] group voiceprints into speaker IDs
        |
        v
    tuple[SpeakerTurn]  (start, end, speaker) in ORIGINAL-time seconds
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from app.core.logging_utils import get_logger

from .types import Sentence, SpeakerTurn

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


class DiarizationApiUnavailableError(RuntimeError):
    """Raised when the installed sherpa-onnx build lacks the diarization API."""


def _build_diarizer(
    seg_onnx: Path,
    emb_onnx: Path,
    *,
    num_speakers: int,
    cluster_threshold: float,
    num_threads: int,
) -> Any:
    import sherpa_onnx

    required = (
        "OfflineSpeakerDiarization",
        "OfflineSpeakerDiarizationConfig",
        "OfflineSpeakerSegmentationModelConfig",
        "OfflineSpeakerSegmentationPyannoteModelConfig",
        "SpeakerEmbeddingExtractorConfig",
        "FastClusteringConfig",
    )
    missing = [name for name in required if not hasattr(sherpa_onnx, name)]
    if missing:
        version = getattr(sherpa_onnx, "__version__", "unknown")
        msg = (
            f"this sherpa-onnx build ({version}) lacks the speaker-diarization API "
            f"(missing: {', '.join(missing)}). Upgrade with: pip install -U 'sherpa-onnx>=1.10'"
        )
        raise DiarizationApiUnavailableError(msg)

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(seg_onnx),
            ),
            num_threads=max(1, num_threads),
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(emb_onnx),
            num_threads=max(1, num_threads),
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_speakers,
            threshold=cluster_threshold,
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        msg = (
            "sherpa-onnx rejected the diarization config; check that the segmentation "
            "and embedding model files exist and are valid."
        )
        raise RuntimeError(msg)
    return sherpa_onnx.OfflineSpeakerDiarization(config)


def diarize_pcm_sync(
    samples: np.ndarray,
    *,
    seg_onnx: Path,
    emb_onnx: Path,
    num_speakers: int = -1,
    cluster_threshold: float = 0.5,
    num_threads: int = 1,
) -> tuple[SpeakerTurn, ...]:
    """Run diarization on 16kHz mono float32 PCM at original speed (1.0x).

    Diarization on sped-up audio degrades both segmentation boundaries and
    embedding quality, so callers MUST pass samples decoded at speed=1.0.
    """
    if samples is None or len(samples) == 0:
        return ()
    contiguous = np.ascontiguousarray(samples, dtype=np.float32)

    sd = _build_diarizer(
        seg_onnx,
        emb_onnx,
        num_speakers=num_speakers,
        cluster_threshold=cluster_threshold,
        num_threads=num_threads,
    )

    result = sd.process(contiguous).sort_by_start_time()
    return tuple(
        SpeakerTurn(start=float(r.start), end=float(r.end), speaker=int(r.speaker)) for r in result
    )


def speaker_at(turns: tuple[SpeakerTurn, ...], t: float) -> int | None:
    """Speaker index whose turn contains ``t``, else the nearest turn's speaker.

    Returns ``None`` only when no turns were produced (silent or failed input).
    """
    if not turns:
        return None
    best: int | None = None
    best_gap: float | None = None
    for turn in turns:
        if turn.start <= t <= turn.end:
            return turn.speaker
        gap = turn.start - t if t < turn.start else t - turn.end
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best = turn.speaker
    return best


def label_sentences(
    sentences: tuple[Sentence, ...],
    turns: tuple[SpeakerTurn, ...],
) -> tuple[tuple[int | None, Sentence], ...]:
    """Attach a speaker index to each sentence; same clock both sides."""
    return tuple((speaker_at(turns, sentence.start_sec), sentence) for sentence in sentences)
