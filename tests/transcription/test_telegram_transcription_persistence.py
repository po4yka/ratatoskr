from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.telegram.command_handlers.transcribe_handler import TranscribeHandler
from app.adapters.telegram.routing.voice_message_processor import VoiceMessageProcessor
from app.adapters.transcription import Sentence, SpeakerTurn, TranscriptionResult
from app.application.ports.transcriptions import (
    TranscriptionArtifactCreate,
    TranscriptionArtifactRecord,
    TranscriptionJobCreate,
    TranscriptionJobRecord,
)
from app.config.transcription import TranscriptionConfig


@dataclass(slots=True)
class _FakeTranscriptionRepository:
    jobs: list[TranscriptionJobCreate]
    artifacts: list[TranscriptionArtifactCreate]
    failures: list[tuple[int, str, str]]

    def __init__(self) -> None:
        self.jobs = []
        self.artifacts = []
        self.failures = []

    async def create_job(self, job: TranscriptionJobCreate) -> TranscriptionJobRecord:
        self.jobs.append(job)
        return TranscriptionJobRecord(
            id=len(self.jobs),
            user_id=job.user_id,
            request_id=job.request_id,
            telegram_chat_id=job.telegram_chat_id,
            telegram_message_id=job.telegram_message_id,
            source_type=job.source_type,
            language=job.language,
            backend=job.backend,
            tokens_mode=job.tokens_mode,
            model_identifier=job.model_identifier,
            status=job.status,
            duration_sec=job.duration_sec,
            audio_hash=job.audio_hash,
            correlation_id=job.correlation_id,
            error_code=None,
            error_message=None,
            metadata_json=job.metadata_json,
            created_at=MagicMock(),
            updated_at=MagicMock(),
        )

    async def complete_job_with_artifact(
        self,
        job_id: int,
        artifact: TranscriptionArtifactCreate,
    ) -> TranscriptionArtifactRecord:
        self.artifacts.append(artifact)
        return TranscriptionArtifactRecord(
            id=len(self.artifacts),
            job_id=job_id,
            user_id=artifact.user_id,
            request_id=artifact.request_id,
            telegram_chat_id=artifact.telegram_chat_id,
            telegram_message_id=artifact.telegram_message_id,
            source_type=artifact.source_type,
            language=artifact.language,
            backend=artifact.backend,
            tokens_mode=artifact.tokens_mode,
            model_identifier=artifact.model_identifier,
            status=artifact.status,
            duration_sec=artifact.duration_sec,
            plain_text=artifact.plain_text,
            sentences_json=artifact.sentences_json,
            speaker_turns_json=artifact.speaker_turns_json,
            audio_hash=artifact.audio_hash,
            correlation_id=artifact.correlation_id,
            metadata_json=artifact.metadata_json,
            created_at=MagicMock(),
        )

    async def fail_job(
        self,
        job_id: int,
        *,
        error_code: str,
        error_message: str,
    ) -> TranscriptionJobRecord | None:
        self.failures.append((job_id, error_code, error_message))
        return None

    async def list_artifacts_for_user(
        self,
        user_id: int,
        *,
        limit: int = 50,
    ) -> list[TranscriptionArtifactRecord]:
        return []


def _cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.transcription = TranscriptionConfig(enabled=True, model_path=Path("/models/asr"))
    return cfg


def _formatter() -> MagicMock:
    formatter = MagicMock()
    formatter.safe_reply = AsyncMock()
    return formatter


def _service(result: TranscriptionResult) -> MagicMock:
    service = MagicMock()
    service.enabled = True
    service.transcribe_media_path = AsyncMock(return_value=result)
    return service


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.text = "/transcribe https://example.com/audio.mp3"
    ctx.uid = 4242
    ctx.correlation_id = "cid-command"
    ctx.message = MagicMock()
    ctx.message.chat_id = 111
    ctx.message.id = 222
    ctx.message.reply_to_message = None
    return ctx


def _media(tmp_path: Path) -> Path:
    path = tmp_path / "voice.ogg"
    path.write_bytes(b"audio-bytes")
    return path


def _expected_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_transcribe_command_persists_mocked_service_artifact(tmp_path: Path) -> None:
    media_path = _media(tmp_path)
    repo = _FakeTranscriptionRepository()
    result = TranscriptionResult(
        plain_text="hello from command",
        sentences=(Sentence(0.0, "hello from command"),),
        speaker_turns=(SpeakerTurn(start=0.0, end=2.0, speaker=0),),
        detected_language="en",
        duration_sec=2.0,
        used_diarization=True,
    )
    formatter = _formatter()
    handler = TranscribeHandler(
        cfg=_cfg(),
        response_formatter=formatter,
        transcription_service=_service(result),
        transcription_repository=repo,
    )

    with patch(
        "app.adapters.telegram.command_handlers.transcribe_handler.fetch_url_to_local_sync",
        return_value=media_path,
    ):
        await handler.handle_transcribe(_ctx())

    assert repo.jobs
    assert repo.artifacts
    assert repo.jobs[0].user_id == 4242
    assert repo.jobs[0].source_type == "url"
    assert repo.jobs[0].audio_hash == _expected_hash(media_path)
    assert repo.artifacts[0].plain_text == "hello from command"
    assert repo.artifacts[0].sentences_json == [{"start_sec": 0.0, "text": "hello from command"}]
    assert repo.artifacts[0].speaker_turns_json == [
        {"start": 0.0, "end": 2.0, "speaker": 0, "label": "SPEAKER_00"}
    ]
    assert str(media_path) not in _serialized(repo.jobs[0])
    assert str(media_path) not in _serialized(repo.artifacts[0])
    sent_texts = [call.args[1] for call in formatter.safe_reply.await_args_list]
    assert any("hello from command" in text for text in sent_texts)


@pytest.mark.asyncio
async def test_auto_voice_path_persists_mocked_service_artifact(tmp_path: Path) -> None:
    media_path = _media(tmp_path)
    repo = _FakeTranscriptionRepository()
    formatter = _formatter()
    processor = VoiceMessageProcessor(
        response_formatter=formatter,
        transcription_service=_service(TranscriptionResult(plain_text="voice transcript")),
        diarization_enabled=False,
        transcription_cfg=TranscriptionConfig(enabled=True, model_path=Path("/models/asr")),
        transcription_repository=repo,
    )
    message = MagicMock(voice=object(), audio=None, video_note=None)
    message.sender_id = 5151
    message.chat_id = 6161
    message.id = 7171
    message.download_media = AsyncMock(return_value=str(media_path))

    handled = await processor.handle(message, correlation_id="cid-voice")

    assert handled is True
    assert repo.jobs[0].user_id == 5151
    assert repo.jobs[0].source_type == "telegram_voice"
    assert repo.jobs[0].telegram_chat_id == 6161
    assert repo.jobs[0].telegram_message_id == 7171
    assert repo.artifacts[0].plain_text == "voice transcript"
    assert repo.artifacts[0].audio_hash == _expected_hash(media_path)
    assert str(media_path) not in _serialized(repo.jobs[0])
    assert str(media_path) not in _serialized(repo.artifacts[0])


def _serialized(value: Any) -> str:
    return repr(value)
