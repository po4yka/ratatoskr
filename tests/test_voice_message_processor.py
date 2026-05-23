"""Unit tests for VoiceMessageProcessor (auto-transcribe voice/audio/video_note)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.telegram.routing.voice_message_processor import (
    VoiceMessageProcessor,
    has_transcribable_voice_media,
)
from app.adapters.transcription import (
    Sentence,
    TranscriptionDurationExceededError,
    TranscriptionResult,
)


def _make_processor(
    *, diarization_enabled: bool = False, service_enabled: bool = True
) -> tuple[VoiceMessageProcessor, AsyncMock, AsyncMock]:
    formatter = MagicMock()
    formatter.safe_reply = AsyncMock()
    service = MagicMock()
    service.enabled = service_enabled
    service.transcribe_media_path = AsyncMock()
    processor = VoiceMessageProcessor(
        response_formatter=formatter,
        transcription_service=service,
        diarization_enabled=diarization_enabled,
    )
    return processor, formatter, service


def _voice_message(saved_path: str = "/tmp/voice.ogg") -> MagicMock:
    msg = MagicMock(
        voice=object(),
        audio=None,
        video_note=None,
    )
    msg.download_media = AsyncMock(return_value=saved_path)
    return msg


def test_has_transcribable_voice_media_discriminates_correctly() -> None:
    assert has_transcribable_voice_media(MagicMock(voice=object(), audio=None, video_note=None))
    assert has_transcribable_voice_media(MagicMock(voice=None, audio=object(), video_note=None))
    assert has_transcribable_voice_media(MagicMock(voice=None, audio=None, video_note=object()))
    assert not has_transcribable_voice_media(
        MagicMock(voice=None, audio=None, video_note=None)
    )


async def test_non_voice_message_falls_through() -> None:
    processor, formatter, service = _make_processor()
    msg = MagicMock(voice=None, audio=None, video_note=None)
    handled = await processor.handle(msg, correlation_id="cid")
    assert handled is False
    formatter.safe_reply.assert_not_awaited()
    service.transcribe_media_path.assert_not_awaited()


async def test_voice_with_disabled_service_falls_through() -> None:
    processor, formatter, service = _make_processor(service_enabled=False)
    handled = await processor.handle(_voice_message(), correlation_id="cid")
    assert handled is False
    formatter.safe_reply.assert_not_awaited()
    service.transcribe_media_path.assert_not_awaited()


async def test_voice_happy_path_replies_with_transcript() -> None:
    processor, formatter, service = _make_processor()
    service.transcribe_media_path.return_value = TranscriptionResult(
        plain_text="hello there",
        sentences=(Sentence(0.0, "hello there."),),
    )

    handled = await processor.handle(_voice_message(), correlation_id="cid")

    assert handled is True
    service.transcribe_media_path.assert_awaited_once()
    sent_texts = [call.args[1] for call in formatter.safe_reply.await_args_list]
    assert any("hello there" in text for text in sent_texts)


async def test_voice_long_media_replies_with_friendly_error() -> None:
    processor, formatter, service = _make_processor()
    service.transcribe_media_path.side_effect = TranscriptionDurationExceededError(
        duration_sec=2400.0, max_duration_sec=1800
    )

    handled = await processor.handle(_voice_message(), correlation_id="cid")

    assert handled is True
    sent_texts = [call.args[1] for call in formatter.safe_reply.await_args_list]
    assert any("2400" in text for text in sent_texts)


async def test_voice_blank_transcript_emits_no_speech_message() -> None:
    processor, formatter, service = _make_processor()
    service.transcribe_media_path.return_value = TranscriptionResult(plain_text="")

    handled = await processor.handle(_voice_message(), correlation_id="cid")

    assert handled is True
    sent_texts = [call.args[1] for call in formatter.safe_reply.await_args_list]
    assert any("no recognizable speech" in text.lower() for text in sent_texts)


@pytest.mark.parametrize("attr_name", ["voice", "audio", "video_note"])
async def test_all_three_media_kinds_trigger_processing(attr_name: str) -> None:
    processor, _formatter, service = _make_processor()
    service.transcribe_media_path.return_value = TranscriptionResult(plain_text="x")

    msg = MagicMock(voice=None, audio=None, video_note=None)
    setattr(msg, attr_name, object())
    msg.download_media = AsyncMock(return_value="/tmp/clip.bin")

    handled = await processor.handle(msg, correlation_id="cid")

    assert handled is True
    service.transcribe_media_path.assert_awaited_once()
