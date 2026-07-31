"""ffmpeg-backed PCM decoding for the transcription adapter.

Synchronous helpers that callers wrap in ``asyncio.to_thread`` -- ffmpeg is a
subprocess shell-out, not async-aware. Mirrors yapsnap's decode path
(``-f s16le -ac 1 -ar 16000`` + a pitch-preserving ``atempo`` cascade).
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import numpy as np

from app.application.ports.transcriptions import PermanentTranscriptionError
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

SAMPLE_RATE = 16000


class FfmpegNotInstalledError(RuntimeError):
    """Raised when the ffmpeg binary cannot be found on PATH."""


class AudioDecodeError(PermanentTranscriptionError, RuntimeError):
    """Raised when ffmpeg fails to decode a media file."""


class NoAudioStreamError(PermanentTranscriptionError, RuntimeError):
    """Raised when the input file carries no audio stream ffmpeg can read."""


def require_ffmpeg() -> None:
    """Raise ``FfmpegNotInstalledError`` if ffmpeg is not on PATH."""
    if shutil.which("ffmpeg") is None:
        msg = "ffmpeg binary not found on PATH; install ffmpeg to enable transcription"
        raise FfmpegNotInstalledError(msg)


# ffprobe only reads container metadata, so it is quick or it is wedged.
_PROBE_TIMEOUT_SEC = 15.0
# A hang guard, not a throughput budget: -nostdin already rules out the classic
# stdin wait, but a corrupt container or a stalled network mount can leave
# ffmpeg running forever. Every one of these calls runs inside asyncio.to_thread
# on the shared default executor, and a lost worker there also starves the
# SSRF preflight and summary parsing that share it.
_DECODE_TIMEOUT_SEC = 1800.0


def has_audio_stream(media_path: Path) -> bool:
    """Return True when ffprobe reports at least one audio stream.

    If ffprobe is missing we skip the check rather than fail loudly, since the
    ffmpeg decode step will produce a clear error if no audio is actually
    present.
    """
    if shutil.which("ffprobe") is None:
        return True
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(media_path),
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SEC,
        )
    except subprocess.CalledProcessError:
        return False
    except subprocess.TimeoutExpired:
        # Same posture as a missing ffprobe: defer to the decode step, which
        # reports a clear error if there really is no audio.
        logger.warning("transcription_ffprobe_timeout", extra={"media_path": str(media_path)})
        return True
    return "audio" in proc.stdout.lower()


def probe_duration_sec(media_path: Path) -> float | None:
    """Return container duration in seconds via ffprobe, or None on failure."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media_path),
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SEC,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _atempo_chain(speed: float) -> list[str]:
    """Return the ffmpeg ``atempo`` filter stages for an arbitrary speed factor.

    A single ``atempo`` stage accepts 0.5..2.0; outside that range we cascade.
    """
    stages: list[str] = []
    remaining = float(speed)
    while remaining > 2.0:
        stages.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        stages.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-5:
        stages.append(f"atempo={remaining}")
    return stages


def decode_to_pcm(media_path: Path, speed: float = 1.0) -> np.ndarray:
    """Decode any media file ffmpeg understands to 16kHz mono float32 PCM.

    `speed` applies the pitch-preserving ``atempo`` filter. Returns an empty
    array if the file decodes to zero samples (e.g. a corrupted clip).
    """
    require_ffmpeg()

    if not has_audio_stream(media_path):
        msg = (
            f"{media_path.name} has no audio stream that ffmpeg can read. "
            "If this is a downloaded social-media clip, the source may have "
            "only offered an audio-stripped 'Playback' format."
        )
        raise NoAudioStreamError(msg)

    cmd: list[str] = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(media_path)]
    if abs(speed - 1.0) > 1e-5:
        stages = _atempo_chain(speed)
        if stages:
            cmd += ["-filter:a", ",".join(stages)]
    cmd += [
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            check=True,
            timeout=_DECODE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            "transcription_ffmpeg_decode_timeout",
            extra={"media_path": str(media_path), "timeout_sec": _DECODE_TIMEOUT_SEC},
        )
        msg = f"ffmpeg decode timed out after {_DECODE_TIMEOUT_SEC:.0f}s for {media_path.name}"
        raise AudioDecodeError(msg) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else ""
        logger.warning(
            "transcription_ffmpeg_decode_failed",
            extra={"media_path": str(media_path), "stderr": stderr[:500]},
        )
        msg = f"ffmpeg decode failed for {media_path.name}: {stderr[:200]}"
        raise AudioDecodeError(msg) from exc

    if not proc.stdout:
        return np.array([], dtype=np.float32)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
