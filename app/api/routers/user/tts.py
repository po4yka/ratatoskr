"""TTS audio generation endpoints for summaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.api.dependencies.database import (
    get_audio_generation_repository,
    get_session_manager,
    get_summary_repository,
    get_user_repository,
)
from app.api.exceptions import FeatureDisabledError, ResourceNotFoundError
from app.api.models.responses import success_response
from app.api.routers.auth import get_current_user
from app.application.services.tts_service import TTSService
from app.config import load_config
from app.infrastructure.audio.elevenlabs_provider import ElevenLabsTTSProviderAdapter
from app.infrastructure.audio.filesystem_storage import FileSystemAudioStorageAdapter

router = APIRouter()
preferences_router = APIRouter()

_TTS_PREFERENCES_KEY = "tts"
_SOURCE_FIELD_PATTERN = "^(summary_250|summary_1000|tldr)$"


class TTSPreferencesUpdateRequest(BaseModel):
    """User-level TTS preference overrides."""

    model_config = ConfigDict(populate_by_name=True)

    voice_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("voiceId", "voice_id"),
        serialization_alias="voiceId",
    )
    model_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("modelName", "model_name"),
        serialization_alias="modelName",
    )
    speed: float | None = Field(default=None, ge=0.5, le=2.0)
    language: str | None = Field(default=None, min_length=2, max_length=20)


class TTSPreferencesResponse(BaseModel):
    """Effective TTS preferences after applying server defaults."""

    model_config = ConfigDict(populate_by_name=True)

    voice_id: str = Field(serialization_alias="voiceId")
    model_name: str = Field(serialization_alias="modelName")
    speed: float
    language: str


class TTSPlaylistRequest(BaseModel):
    """Request body for generating an ordered audio playlist."""

    model_config = ConfigDict(populate_by_name=True)

    summary_ids: list[int] = Field(
        min_length=1,
        max_length=50,
        validation_alias=AliasChoices("summaryIds", "summary_ids"),
        serialization_alias="summaryIds",
    )
    source_field: str = Field(
        default="summary_1000",
        pattern=_SOURCE_FIELD_PATTERN,
        validation_alias=AliasChoices("sourceField", "source_field"),
        serialization_alias="sourceField",
    )


def _get_tts_config() -> Any:
    return load_config(allow_stub_telegram=True).tts


async def _get_tts_service(request: Request, user_id: int) -> TTSService:
    config = await _get_effective_tts_config(request, user_id)
    db = get_session_manager(request)
    return TTSService(
        summary_repository=get_summary_repository(db, request),
        audio_generation_repository=get_audio_generation_repository(db, request),
        tts_provider=ElevenLabsTTSProviderAdapter(config),
        audio_storage=FileSystemAudioStorageAdapter(config.audio_storage_path),
        voice_id=config.voice_id,
        model_name=config.model,
        max_chars_per_request=config.max_chars_per_request,
    )


async def _get_effective_tts_config(request: Request, user_id: int) -> Any:
    config = _get_tts_config()
    preferences = await _get_effective_tts_preferences(request, user_id)
    return config.model_copy(
        update={
            "voice_id": preferences.voice_id,
            "model": preferences.model_name,
            "speed": preferences.speed,
        }
    )


async def _get_effective_tts_preferences(request: Request, user_id: int) -> TTSPreferencesResponse:
    user_record = await get_user_repository(
        get_session_manager(request), request
    ).async_get_user_by_telegram_id(user_id)
    return _effective_tts_preferences_from_record(user_record)


def _effective_tts_preferences_from_record(
    user_record: dict[str, Any] | None,
) -> TTSPreferencesResponse:
    config = _get_tts_config()
    stored = _stored_tts_preferences(user_record)
    return TTSPreferencesResponse(
        voice_id=_non_empty_str(stored.get("voice_id")) or config.voice_id,
        model_name=_non_empty_str(stored.get("model_name")) or config.model,
        speed=_valid_speed(stored.get("speed"), default=config.speed),
        language=_non_empty_str(stored.get("language")) or "auto",
    )


def _stored_tts_preferences(user_record: dict[str, Any] | None) -> dict[str, Any]:
    preferences = user_record.get("preferences_json") if user_record else None
    if not isinstance(preferences, dict):
        return {}
    tts_preferences = preferences.get(_TTS_PREFERENCES_KEY)
    return dict(tts_preferences) if isinstance(tts_preferences, dict) else {}


def _non_empty_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _valid_speed(value: Any, *, default: float) -> float:
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return default
    return speed if 0.5 <= speed <= 2.0 else default


async def _ensure_summary_owned(summary_id: int, user_id: int, request: Request) -> None:
    summary = await get_summary_repository(
        get_session_manager(request), request
    ).async_get_summary_by_id(summary_id)
    if summary is None or summary.get("user_id") != user_id:
        raise ResourceNotFoundError("Summary", summary_id)


def _audio_url(summary_id: int) -> str:
    return f"/v1/summaries/{summary_id}/audio"


@preferences_router.get("/me/tts-preferences")
async def get_tts_preferences(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Get the current user's effective TTS preferences."""
    preferences = await _get_effective_tts_preferences(request, user["user_id"])
    return success_response(preferences)


@preferences_router.put("/me/tts-preferences")
async def update_tts_preferences(
    preferences: TTSPreferencesUpdateRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Update or clear the current user's TTS preference overrides."""
    user_repo = get_user_repository(get_session_manager(request), request)
    user_record, _created = await user_repo.async_get_or_create_user(
        user["user_id"],
        username=user.get("username"),
        is_owner=False,
    )
    stored_preferences = user_record.get("preferences_json")
    current_preferences = dict(stored_preferences) if isinstance(stored_preferences, dict) else {}
    current_tts = _stored_tts_preferences(user_record)

    updates = preferences.model_dump(exclude_unset=True, by_alias=False)
    for key, value in updates.items():
        if value is None:
            current_tts.pop(key, None)
        else:
            current_tts[key] = value

    if current_tts:
        current_preferences[_TTS_PREFERENCES_KEY] = current_tts
    else:
        current_preferences.pop(_TTS_PREFERENCES_KEY, None)
    await user_repo.async_update_user_preferences(user["user_id"], current_preferences)

    updated_record = await user_repo.async_get_user_by_telegram_id(user["user_id"])
    return success_response(_effective_tts_preferences_from_record(updated_record))


@router.post("/{summary_id}/audio")
async def generate_audio(
    summary_id: int,
    request: Request,
    source_field: str = Query("summary_1000", pattern=_SOURCE_FIELD_PATTERN),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Generate audio for a summary, reusing cached output when available."""
    tts_config = _get_tts_config()
    if not tts_config.enabled:
        raise FeatureDisabledError("tts")

    await _ensure_summary_owned(summary_id, user["user_id"], request)
    service = await _get_tts_service(request, user["user_id"])
    try:
        result = await service.generate_audio(summary_id, source_field=source_field)
    finally:
        await service.close()

    return success_response(
        {
            "summaryId": summary_id,
            "status": result.status,
            "charCount": result.char_count,
            "fileSizeBytes": result.file_size_bytes,
            "latencyMs": result.latency_ms,
            "error": result.error,
        }
    )


@router.post("/audio/playlist")
async def generate_audio_playlist(
    playlist: TTSPlaylistRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Generate an ordered playlist manifest for multiple summaries."""
    tts_config = _get_tts_config()
    if not tts_config.enabled:
        raise FeatureDisabledError("tts")

    service = await _get_tts_service(request, user["user_id"])
    items: list[dict[str, Any]] = []
    try:
        for position, summary_id in enumerate(playlist.summary_ids):
            await _ensure_summary_owned(summary_id, user["user_id"], request)
            result = await service.generate_audio(summary_id, source_field=playlist.source_field)
            items.append(
                {
                    "summaryId": summary_id,
                    "position": position,
                    "status": result.status,
                    "audioUrl": _audio_url(summary_id) if result.status == "completed" else None,
                    "charCount": result.char_count,
                    "fileSizeBytes": result.file_size_bytes,
                    "latencyMs": result.latency_ms,
                    "error": result.error,
                }
            )
    finally:
        await service.close()

    return success_response({"items": items})


@router.get("/{summary_id}/audio")
async def get_audio(
    summary_id: int,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> FileResponse:
    """Stream/download the generated audio file for a summary."""
    tts_config = _get_tts_config()
    if not tts_config.enabled:
        raise FeatureDisabledError("tts")

    await _ensure_summary_owned(summary_id, user["user_id"], request)
    service = await _get_tts_service(request, user["user_id"])
    try:
        result = await service.get_audio_status(summary_id)
    finally:
        await service.close()

    if result is None or result.status != "completed" or not result.file_path:
        raise ResourceNotFoundError("audio", summary_id)

    file_path = Path(result.file_path)
    if not file_path.is_file():
        raise ResourceNotFoundError("audio_file", summary_id)

    return FileResponse(
        path=str(file_path),
        media_type="audio/mpeg",
        filename=f"summary-{summary_id}.mp3",
    )
