"""Unit tests for the application-layer TTS service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from app.application.dto.audio_generation import StoredAudioFileDTO
from app.application.services.tts_service import AudioGenerationResult, TTSService

if TYPE_CHECKING:
    from app.application.ports import SummaryRepositoryPort


@dataclass
class _FakeSummaryRepo:
    rows: dict[int, dict]

    async def async_get_summary_by_id(self, summary_id: int) -> dict | None:
        return self.rows.get(summary_id)


@dataclass
class _FakeAudioRepo:
    completed: dict[tuple[int, str], dict]
    latest: dict[int, dict]
    started: list[dict]
    completed_calls: list[dict]
    failed_calls: list[dict]

    async def async_get_completed_generation(
        self,
        summary_id: int,
        source_field: str,
        *,
        voice_id: str | None = None,
        model_name: str | None = None,
    ) -> dict | None:
        generation = self.completed.get((summary_id, source_field))
        if generation is None:
            return None
        if voice_id is not None and generation.get("voice_id") != voice_id:
            return None
        if model_name is not None and generation.get("model") != model_name:
            return None
        return generation

    async def async_get_latest_generation(self, summary_id: int) -> dict | None:
        return self.latest.get(summary_id)

    async def async_mark_generation_started(self, **kwargs) -> None:
        self.started.append(kwargs)
        self.latest[kwargs["summary_id"]] = {"status": "generating", **kwargs}

    async def async_mark_generation_completed(self, **kwargs) -> None:
        self.completed_calls.append(kwargs)
        self.completed[(kwargs["summary_id"], kwargs["source_field"])] = {
            "status": "completed",
            **kwargs,
        }
        self.latest[kwargs["summary_id"]] = {"status": "completed", **kwargs}

    async def async_mark_generation_failed(self, **kwargs) -> None:
        self.failed_calls.append(kwargs)
        self.latest[kwargs["summary_id"]] = {
            "status": "error",
            **kwargs,
            "error_text": kwargs["error_text"],
        }


class _FakeProvider:
    def __init__(self, result: bytes | Exception) -> None:
        self._result = result
        self.calls: list[dict] = []
        self.closed = False

    async def synthesize(self, text: str, *, use_long_form: bool = False) -> bytes:
        self.calls.append({"text": text, "use_long_form": use_long_form})
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def close(self) -> None:
        self.closed = True


class _FakeStorage:
    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    async def save_audio(self, summary_id: int, audio_bytes: bytes) -> StoredAudioFileDTO:
        path = self._tmp_path / f"{summary_id}.mp3"
        path.write_bytes(audio_bytes)
        return StoredAudioFileDTO(file_path=str(path), file_size_bytes=len(audio_bytes))


def _build_service(
    *,
    tmp_path: Path,
    summaries: dict[int, dict] | None = None,
    completed: dict[tuple[int, str], dict] | None = None,
    latest: dict[int, dict] | None = None,
    provider_result: bytes | Exception = b"audio",
) -> tuple[TTSService, _FakeAudioRepo, _FakeProvider]:
    audio_repo = _FakeAudioRepo(
        completed=completed or {},
        latest=latest or {},
        started=[],
        completed_calls=[],
        failed_calls=[],
    )
    provider = _FakeProvider(provider_result)
    service = TTSService(
        summary_repository=cast("SummaryRepositoryPort", _FakeSummaryRepo(summaries or {})),
        audio_generation_repository=audio_repo,
        tts_provider=provider,
        audio_storage=_FakeStorage(tmp_path),
        voice_id="voice-123",
        model_name="eleven-model",
        max_chars_per_request=20,
    )
    return service, audio_repo, provider


@pytest.mark.asyncio
async def test_returns_cached_result_when_completed_file_exists(tmp_path: Path) -> None:
    file_path = tmp_path / "1.mp3"
    file_path.write_bytes(b"cached")
    service, _, provider = _build_service(
        tmp_path=tmp_path,
        completed={
            (1, "summary_1000"): {
                "file_path": str(file_path),
                "file_size_bytes": 6,
                "char_count": 10,
                "latency_ms": 100,
                "voice_id": "voice-123",
                "model": "eleven-model",
            }
        },
    )

    result = await service.generate_audio(1)

    assert result.status == "completed"
    assert result.file_path == str(file_path)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_ignores_cached_result_for_different_voice_or_model(tmp_path: Path) -> None:
    file_path = tmp_path / "1.mp3"
    file_path.write_bytes(b"cached")
    service, _, provider = _build_service(
        tmp_path=tmp_path,
        summaries={1: {"id": 1, "lang": "en", "json_payload": {"summary_1000": "Fresh text"}}},
        completed={
            (1, "summary_1000"): {
                "file_path": str(file_path),
                "file_size_bytes": 6,
                "char_count": 10,
                "latency_ms": 100,
                "voice_id": "other-voice",
                "model": "eleven-model",
            }
        },
        provider_result=b"fresh-audio",
    )

    result = await service.generate_audio(1)

    assert result.status == "completed"
    assert provider.calls[0]["text"] == "Fresh text"


@pytest.mark.asyncio
async def test_returns_error_when_summary_not_found(tmp_path: Path) -> None:
    service, _, _ = _build_service(tmp_path=tmp_path)

    result = await service.generate_audio(999)

    assert result.status == "error"
    assert result.error == "Summary not found"


@pytest.mark.asyncio
async def test_falls_back_through_source_field_chain(tmp_path: Path) -> None:
    service, _, provider = _build_service(
        tmp_path=tmp_path,
        summaries={
            1: {
                "id": 1,
                "lang": "en",
                "json_payload": {"summary_1000": "", "summary_250": "Fallback text.", "tldr": ""},
            }
        },
        provider_result=b"generated",
    )

    result = await service.generate_audio(1, source_field="summary_1000")

    assert result.status == "completed"
    assert provider.calls[0]["text"] == "Fallback text."
    assert result.char_count == len("Fallback text.")


@pytest.mark.asyncio
async def test_on_provider_error_updates_generation_status(tmp_path: Path) -> None:
    service, audio_repo, _ = _build_service(
        tmp_path=tmp_path,
        summaries={1: {"id": 1, "lang": "en", "json_payload": {"summary_1000": "Text"}}},
        provider_result=RuntimeError("boom"),
    )

    result = await service.generate_audio(1)

    assert result.status == "error"
    assert result.error == "boom"
    assert audio_repo.failed_calls[0]["summary_id"] == 1


@pytest.mark.asyncio
async def test_on_success_writes_mp3_and_updates_generation(tmp_path: Path) -> None:
    service, audio_repo, provider = _build_service(
        tmp_path=tmp_path,
        summaries={
            1: {"id": 1, "lang": "en", "json_payload": {"summary_1000": "Long enough text"}}
        },
        provider_result=b"real-audio",
    )

    result = await service.generate_audio(1)

    assert result.status == "completed"
    assert Path(result.file_path or "").read_bytes() == b"real-audio"
    assert audio_repo.completed_calls[0]["file_size_bytes"] == len(b"real-audio")
    assert provider.calls[0]["text"] == "Long enough text"


@pytest.mark.asyncio
async def test_get_audio_status_returns_none_when_no_row(tmp_path: Path) -> None:
    service, _, _ = _build_service(tmp_path=tmp_path)

    assert await service.get_audio_status(42) is None


@pytest.mark.asyncio
async def test_get_audio_status_returns_result_when_row_exists(tmp_path: Path) -> None:
    service, _, _ = _build_service(
        tmp_path=tmp_path,
        latest={
            42: {
                "status": "completed",
                "file_path": "/data/audio/42.mp3",
                "file_size_bytes": 1024,
                "char_count": 200,
                "latency_ms": 1500,
                "error_text": None,
            }
        },
    )

    result = await service.get_audio_status(42)

    assert isinstance(result, AudioGenerationResult)
    assert result.status == "completed"
    assert result.file_path == "/data/audio/42.mp3"
