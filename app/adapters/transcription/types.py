"""Public dataclasses for the transcription adapter."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Sentence:
    """A single recognized sentence with its original-audio start time."""

    start_sec: float
    text: str


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    """A contiguous span attributed to one speaker, in original-time seconds."""

    start: float
    end: float
    speaker: int

    @property
    def label(self) -> str:
        return f"SPEAKER_{self.speaker:02d}"


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """Final output of a transcription run.

    `plain_text` is always populated. `sentences` is populated when the
    sherpa-onnx build exposes timestamp/token data (the recommended path);
    otherwise it is an empty tuple and only `plain_text` should be used.
    `speaker_turns` is populated only when diarization ran.
    """

    plain_text: str
    sentences: tuple[Sentence, ...] = ()
    speaker_turns: tuple[SpeakerTurn, ...] = ()
    detected_language: str | None = None
    duration_sec: float | None = None
    used_diarization: bool = False
