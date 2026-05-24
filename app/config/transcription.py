"""Configuration for the CPU-only transcription adapter (sherpa-onnx + ffmpeg).

Off by default. Enable with ``TRANSCRIPTION_ENABLED=true`` and (optionally)
``TRANSCRIPTION_DIARIZATION_ENABLED=true`` for speaker labels.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.core.logging_utils import get_logger

LOGGER = get_logger(__name__)

Language = Literal["en", "ru"]
Backend = Literal["streaming", "offline_transducer"]
TokensMode = Literal["bpe", "char"]

_DEFAULT_MODEL_PATH = "/data/models/transcription"
_DEFAULT_DIARIZATION_PATH = "/data/models/diarization"
_DEFAULT_EMBEDDING_FILE = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"

# Per-language presets pick the right backend + tokens mode automatically so
# users only need to flip ``TRANSCRIPTION_LANGUAGE``. Power users can still
# override via ``TRANSCRIPTION_BACKEND`` / ``TRANSCRIPTION_TOKENS_MODE``.
_LANGUAGE_PRESETS: dict[Language, tuple[Backend, TokensMode]] = {
    "en": ("streaming", "bpe"),  # Kroko streaming Zipformer
    "ru": ("offline_transducer", "char"),  # GigaAM-v3 e2e RNN-T
}


class TranscriptionConfig(BaseModel):
    """Settings for the optional CPU transcription adapter.

    The defaults keep transcription opt-in. ``model_path`` and
    ``diarization_model_path`` are the on-disk locations where the
    auto-downloader caches the sherpa-onnx ASR + diarization models on first
    use; mount these as a persistent volume in production so the ~80 MB ASR
    bundle does not need to re-download on every container restart.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=False,
        validation_alias="TRANSCRIPTION_ENABLED",
        description="Master switch for the transcription adapter",
    )

    language: Language = Field(
        default="en",
        validation_alias="TRANSCRIPTION_LANGUAGE",
        description=(
            "Primary language. 'en' uses the Kroko streaming Zipformer "
            "(streaming, BPE tokens, ~80 MB). 'ru' uses GigaAM-v3 e2e RNN-T "
            "(offline, char-level Cyrillic tokens with built-in punctuation, "
            "~230 MB INT8, MIT license)."
        ),
    )

    backend_override: Backend | None = Field(
        default=None,
        validation_alias="TRANSCRIPTION_BACKEND",
        description=(
            "Override the engine backend selected by language. 'streaming' "
            "uses sherpa_onnx.OnlineRecognizer; 'offline_transducer' uses "
            "OfflineRecognizer.from_transducer. Leave unset to follow the "
            "language preset."
        ),
    )

    tokens_mode_override: TokensMode | None = Field(
        default=None,
        validation_alias="TRANSCRIPTION_TOKENS_MODE",
        description=(
            "Override the tokens-mode selected by language. 'bpe' honours "
            "the U+2581 word-start marker; 'char' concatenates character "
            "tokens verbatim. Leave unset to follow the language preset."
        ),
    )

    model_path: Path = Field(
        default=Path(_DEFAULT_MODEL_PATH),
        validation_alias="TRANSCRIPTION_MODEL_PATH",
        description=(
            "Directory holding the sherpa-onnx model "
            "(encoder/decoder/joiner/tokens.txt). If the directory is empty "
            "the model bundle for the configured TRANSCRIPTION_LANGUAGE is "
            "downloaded into it on first use; if it already contains "
            "tokens.txt it is treated as a custom model."
        ),
    )

    speed: float = Field(
        default=1.5,
        validation_alias="TRANSCRIPTION_SPEED",
        description=(
            "Pre-transcription audio speedup factor (pitch preserved). 1.5x "
            "shaves about a third off CPU time with minimal accuracy cost."
        ),
    )

    num_threads: int = Field(
        default=2,
        validation_alias="TRANSCRIPTION_NUM_THREADS",
        description="Threads sherpa-onnx may use for ASR and diarization inference",
    )

    max_duration_sec: int = Field(
        default=1800,
        validation_alias="TRANSCRIPTION_MAX_DURATION_SEC",
        description=(
            "Refuse any media longer than this many seconds (default 30 min). "
            "Protects the bot from a runaway multi-hour transcribe job."
        ),
    )

    diarization_enabled: bool = Field(
        default=False,
        validation_alias="TRANSCRIPTION_DIARIZATION_ENABLED",
        description=(
            "Enable speaker-label output (SPEAKER_00, SPEAKER_01, ...) using "
            "an additional ONNX segmentation + embedding pass."
        ),
    )

    diarization_model: Literal["pyannote", "reverb"] = Field(
        default="pyannote",
        validation_alias="TRANSCRIPTION_DIARIZATION_MODEL",
        description=(
            "Segmentation model: 'pyannote' (CC-BY-4.0, default) or 'reverb' "
            "(more accurate, NON-COMMERCIAL license)."
        ),
    )

    diarization_model_path: Path = Field(
        default=Path(_DEFAULT_DIARIZATION_PATH),
        validation_alias="TRANSCRIPTION_DIARIZATION_PATH",
        description="Directory holding the diarization segmentation + embedding ONNX files",
    )

    embedding_model_filename: str = Field(
        default=_DEFAULT_EMBEDDING_FILE,
        validation_alias="TRANSCRIPTION_EMBEDDING_MODEL",
        description=(
            "Filename of the speaker-embedding ONNX in the sherpa-onnx "
            "speaker-recongition-models release (note upstream typo)."
        ),
    )

    diarization_cluster_threshold: float = Field(
        default=0.5,
        validation_alias="TRANSCRIPTION_DIARIZATION_CLUSTER_THRESHOLD",
        description=(
            "FastClustering threshold used only when default_num_speakers=-1 "
            "(auto). Higher = fewer, more-merged speakers."
        ),
    )

    default_num_speakers: int = Field(
        default=-1,
        validation_alias="TRANSCRIPTION_DEFAULT_NUM_SPEAKERS",
        description=(
            "Default speaker count for diarization (-1 = auto-detect). "
            "Auto detection degrades above ~7 speakers."
        ),
    )

    auto_on_voice_message: bool = Field(
        default=True,
        validation_alias="TRANSCRIPTION_AUTO_VOICE",
        description=(
            "When TRANSCRIPTION_ENABLED, transcribe Telegram voice/audio/video_note "
            "messages automatically without requiring a /transcribe command."
        ),
    )

    auto_in_url_pipeline: bool = Field(
        default=False,
        validation_alias="TRANSCRIPTION_AUTO_URL_PIPELINE",
        description=(
            "When TRANSCRIPTION_ENABLED, automatically fill "
            "VideoSourceRequest.audio_transcript_text in the YouTube / video "
            "scraper pipeline when the platform-native transcript path returns nothing."
        ),
    )

    @field_validator("speed", mode="before")
    @classmethod
    def _parse_speed(cls, value: Any) -> float:
        if value in (None, ""):
            return 1.5
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            msg = "TRANSCRIPTION_SPEED must be a positive float"
            raise ValueError(msg) from exc
        if parsed <= 0:
            msg = "TRANSCRIPTION_SPEED must be > 0"
            raise ValueError(msg)
        return parsed

    @field_validator(
        "num_threads",
        "max_duration_sec",
        "default_num_speakers",
        mode="before",
    )
    @classmethod
    def _parse_int_fields(cls, value: Any, info: ValidationInfo) -> int:
        if value in (None, ""):
            default = cls.model_fields[info.field_name].default
            return int(default)
        try:
            return int(str(value))
        except ValueError as exc:
            msg = f"{info.field_name.replace('_', ' ')} must be a valid integer"
            raise ValueError(msg) from exc

    @field_validator("diarization_cluster_threshold", mode="before")
    @classmethod
    def _parse_threshold(cls, value: Any) -> float:
        if value in (None, ""):
            return 0.5
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            msg = "TRANSCRIPTION_DIARIZATION_CLUSTER_THRESHOLD must be a float"
            raise ValueError(msg) from exc

    @field_validator("model_path", "diarization_model_path", mode="before")
    @classmethod
    def _parse_path(cls, value: Any, info: ValidationInfo) -> Path:
        if value in (None, ""):
            return Path(cls.model_fields[info.field_name].default)
        return Path(str(value)).expanduser()

    @field_validator("diarization_model", mode="before")
    @classmethod
    def _parse_diarization_model(cls, value: Any) -> str:
        if value in (None, ""):
            return "pyannote"
        normalized = str(value).strip().lower()
        if normalized not in {"pyannote", "reverb"}:
            msg = "TRANSCRIPTION_DIARIZATION_MODEL must be 'pyannote' or 'reverb'"
            raise ValueError(msg)
        return normalized

    @field_validator("language", mode="before")
    @classmethod
    def _parse_language(cls, value: Any) -> str:
        if value in (None, ""):
            return "en"
        normalized = str(value).strip().lower()
        if normalized not in _LANGUAGE_PRESETS:
            msg = (
                f"TRANSCRIPTION_LANGUAGE must be one of {sorted(_LANGUAGE_PRESETS)}; got {value!r}"
            )
            raise ValueError(msg)
        return normalized

    @field_validator("backend_override", "tokens_mode_override", mode="before")
    @classmethod
    def _parse_optional_enum(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        return str(value).strip().lower()

    @property
    def backend(self) -> Backend:
        """Resolved backend: override if set, otherwise the language preset."""
        return self.backend_override or _LANGUAGE_PRESETS[self.language][0]

    @property
    def tokens_mode(self) -> TokensMode:
        """Resolved tokens mode: override if set, otherwise the language preset."""
        return self.tokens_mode_override or _LANGUAGE_PRESETS[self.language][1]
