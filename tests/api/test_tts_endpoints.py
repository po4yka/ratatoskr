"""Tests for TTS preference and playlist API handlers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request

from app.api.routers.user import tts as tts_router
from app.application.dto.audio_generation import AudioGenerationResult


class _FakeTTSConfig(SimpleNamespace):
    def model_copy(self, *, update: dict[str, Any]) -> _FakeTTSConfig:
        values = dict(self.__dict__)
        values.update(update)
        return _FakeTTSConfig(**values)


class _FakeUserRepository:
    def __init__(self, record: dict[str, Any] | None = None) -> None:
        self.record = record
        self.updated_preferences: dict[str, Any] | None = None

    async def async_get_user_by_telegram_id(self, telegram_user_id: int) -> dict[str, Any] | None:
        return self.record

    async def async_get_or_create_user(
        self,
        telegram_user_id: int,
        *,
        username: str | None = None,
        is_owner: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        if self.record is None:
            self.record = {
                "telegram_user_id": telegram_user_id,
                "username": username,
                "preferences_json": {},
            }
            return self.record, True
        return self.record, False

    async def async_update_user_preferences(
        self,
        telegram_user_id: int,
        preferences: dict[str, Any],
    ) -> None:
        self.updated_preferences = preferences
        self.record = {
            **(self.record or {}),
            "telegram_user_id": telegram_user_id,
            "preferences_json": preferences,
        }


class _FakeTTSService:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.closed = False

    async def generate_audio(
        self,
        summary_id: int,
        *,
        source_field: str = "summary_1000",
    ) -> AudioGenerationResult:
        self.calls.append(summary_id)
        return AudioGenerationResult(
            summary_id=summary_id,
            status="completed",
            file_size_bytes=summary_id * 10,
            char_count=summary_id * 100,
            latency_ms=summary_id,
        )

    async def close(self) -> None:
        self.closed = True


def _config() -> _FakeTTSConfig:
    return _FakeTTSConfig(
        enabled=True,
        voice_id="default-voice",
        model="default-model",
        speed=1.0,
        max_chars_per_request=5000,
        audio_storage_path="/tmp/audio",
    )


@pytest.mark.asyncio
async def test_tts_preferences_round_trip_and_default_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeUserRepository({"telegram_user_id": 123, "preferences_json": {}})
    monkeypatch.setattr(tts_router, "_get_tts_config", _config)
    monkeypatch.setattr(tts_router, "get_session_manager", lambda request: object())
    monkeypatch.setattr(tts_router, "get_user_repository", lambda db, request=None: repo)

    default_response = await tts_router.get_tts_preferences(
        cast("Request", object()),
        user={"user_id": 123, "username": "reader"},
    )
    assert default_response["data"] == {
        "voiceId": "default-voice",
        "modelName": "default-model",
        "speed": 1.0,
        "language": "auto",
    }

    updated_response = await tts_router.update_tts_preferences(
        tts_router.TTSPreferencesUpdateRequest(
            voice_id="custom-voice",
            model_name="custom-model",
            speed=1.25,
            language="en",
        ),
        cast("Request", object()),
        user={"user_id": 123, "username": "reader"},
    )

    assert repo.updated_preferences == {
        "tts": {
            "voice_id": "custom-voice",
            "model_name": "custom-model",
            "speed": 1.25,
            "language": "en",
        }
    }
    assert updated_response["data"] == {
        "voiceId": "custom-voice",
        "modelName": "custom-model",
        "speed": 1.25,
        "language": "en",
    }


@pytest.mark.asyncio
async def test_audio_playlist_returns_urls_in_requested_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeTTSService()
    owned: list[int] = []

    async def fake_ensure_summary_owned(summary_id: int, user_id: int, request: Request) -> None:
        owned.append(summary_id)

    async def fake_get_tts_service(request: Request, user_id: int) -> _FakeTTSService:
        return service

    monkeypatch.setattr(tts_router, "_get_tts_config", _config)
    monkeypatch.setattr(tts_router, "_ensure_summary_owned", fake_ensure_summary_owned)
    monkeypatch.setattr(tts_router, "_get_tts_service", fake_get_tts_service)

    response = await tts_router.generate_audio_playlist(
        tts_router.TTSPlaylistRequest(summary_ids=[7, 3, 11], source_field="tldr"),
        cast("Request", object()),
        user={"user_id": 123},
    )

    assert owned == [7, 3, 11]
    assert service.calls == [7, 3, 11]
    assert service.closed is True
    assert [item["summaryId"] for item in response["data"]["items"]] == [7, 3, 11]
    assert [item["audioUrl"] for item in response["data"]["items"]] == [
        "/v1/summaries/7/audio",
        "/v1/summaries/3/audio",
        "/v1/summaries/11/audio",
    ]
