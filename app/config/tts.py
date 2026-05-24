"""ElevenLabs TTS configuration."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._secret_marker import SECRET_MARKER


class ElevenLabsConfig(BaseModel):
    """ElevenLabs text-to-speech integration configuration."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(default=False, validation_alias="ELEVENLABS_ENABLED")
    api_key: str = Field(
        default="", validation_alias="ELEVENLABS_API_KEY", json_schema_extra=SECRET_MARKER
    )
    voice_id: str = Field(
        default="21m00Tcm4TlvDq8ikWAM",
        validation_alias="ELEVENLABS_VOICE_ID",
        description="Default voice ID (Rachel)",
    )
    model: str = Field(
        default="eleven_multilingual_v2",
        validation_alias="ELEVENLABS_MODEL",
        description="TTS model ID",
    )
    output_format: str = Field(
        default="mp3_44100_128",
        validation_alias="ELEVENLABS_OUTPUT_FORMAT",
    )
    stability: float = Field(
        default=0.5,
        validation_alias="ELEVENLABS_STABILITY",
        description="Voice stability (0.0-1.0)",
    )
    similarity_boost: float = Field(
        default=0.75,
        validation_alias="ELEVENLABS_SIMILARITY_BOOST",
        description="Voice similarity boost (0.0-1.0)",
    )
    speed: float = Field(
        default=1.0,
        validation_alias="ELEVENLABS_SPEED",
        description="Speech speed (0.5-2.0)",
    )
    timeout_sec: float = Field(
        default=60.0,
        validation_alias="ELEVENLABS_TIMEOUT_SEC",
    )
    max_chars_per_request: int = Field(
        default=5000,
        validation_alias="ELEVENLABS_MAX_CHARS",
        description="Character limit per API request (chunking threshold)",
    )
    audio_storage_path: str = Field(
        default="/data/audio",
        validation_alias="ELEVENLABS_AUDIO_PATH",
        description="Directory for cached audio files",
    )

    @field_validator("api_key", mode="before")
    @classmethod
    def _validate_api_key(cls, value: Any) -> str:
        if value in (None, ""):
            return ""
        key = str(value).strip()
        if len(key) > 500:
            msg = "ElevenLabs API key appears to be too long"
            raise ValueError(msg)
        return key

    @field_validator("stability", "similarity_boost", mode="before")
    @classmethod
    def _validate_zero_one(cls, value: Any) -> float:
        if value in (None, ""):
            return 0.5
        try:
            parsed = float(str(value))
        except ValueError as exc:
            msg = "Value must be a valid number"
            raise ValueError(msg) from exc
        if parsed < 0.0 or parsed > 1.0:
            msg = "Value must be between 0.0 and 1.0"
            raise ValueError(msg)
        return parsed

    @field_validator("speed", mode="before")
    @classmethod
    def _validate_speed(cls, value: Any) -> float:
        if value in (None, ""):
            return 1.0
        try:
            parsed = float(str(value))
        except ValueError as exc:
            msg = "Speed must be a valid number"
            raise ValueError(msg) from exc
        if parsed < 0.5 or parsed > 2.0:
            msg = "Speed must be between 0.5 and 2.0"
            raise ValueError(msg)
        return parsed

    @field_validator("timeout_sec", mode="before")
    @classmethod
    def _validate_timeout(cls, value: Any) -> float:
        if value in (None, ""):
            return 60.0
        try:
            parsed = float(str(value))
        except ValueError as exc:
            msg = "Timeout must be a valid number"
            raise ValueError(msg) from exc
        if parsed < 5.0 or parsed > 300.0:
            msg = "Timeout must be between 5 and 300 seconds"
            raise ValueError(msg)
        return parsed

    @field_validator("max_chars_per_request", mode="before")
    @classmethod
    def _validate_max_chars(cls, value: Any) -> int:
        if value in (None, ""):
            return 5000
        try:
            parsed = int(str(value))
        except ValueError as exc:
            msg = "Max chars must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 100 or parsed > 10000:
            msg = "Max chars must be between 100 and 10000"
            raise ValueError(msg)
        return parsed
