"""Unit tests for TranscribeHandler (the /transcribe Telegram command)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.telegram.command_handlers.transcribe_handler import (
    TranscribeHandler,
    _format_transcript,
    _has_transcribable_media,
)
from app.adapters.transcription import (
    Sentence,
    SpeakerTurn,
    TranscriptionDisabledError,
    TranscriptionDurationExceededError,
    TranscriptionResult,
)
from app.config.transcription import TranscriptionConfig


def _make_handler(
    *, diarization_enabled: bool = False, service_enabled: bool = True
) -> tuple[TranscribeHandler, AsyncMock, AsyncMock]:
    cfg = MagicMock()
    cfg.transcription = TranscriptionConfig(
        enabled=service_enabled,
        diarization_enabled=diarization_enabled,
    )
    formatter = MagicMock()
    formatter.safe_reply = AsyncMock()
    service = MagicMock()
    service.enabled = service_enabled
    service.transcribe_media_path = AsyncMock()
    handler = TranscribeHandler(
        cfg=cfg,
        response_formatter=formatter,
        transcription_service=service,
    )
    return handler, formatter, service


def _make_ctx(text: str, *, reply: object | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.text = text
    ctx.uid = 1
    ctx.correlation_id = "test-cid"
    ctx.message = MagicMock()
    ctx.message.reply_to_message = reply
    return ctx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_has_transcribable_media_recognises_voice_audio_video() -> None:
    voice = MagicMock(voice=object(), audio=None, video_note=None, video=None, document=None)
    audio = MagicMock(voice=None, audio=object(), video_note=None, video=None, document=None)
    note = MagicMock(voice=None, audio=None, video_note=object(), video=None, document=None)
    video = MagicMock(voice=None, audio=None, video_note=None, video=object(), document=None)
    audio_doc = MagicMock(
        voice=None,
        audio=None,
        video_note=None,
        video=None,
        document=MagicMock(mime_type="audio/ogg"),
    )
    other_doc = MagicMock(
        voice=None,
        audio=None,
        video_note=None,
        video=None,
        document=MagicMock(mime_type="application/pdf"),
    )
    nothing = MagicMock(voice=None, audio=None, video_note=None, video=None, document=None)

    assert _has_transcribable_media(voice)
    assert _has_transcribable_media(audio)
    assert _has_transcribable_media(note)
    assert _has_transcribable_media(video)
    assert _has_transcribable_media(audio_doc)
    assert not _has_transcribable_media(other_doc)
    assert not _has_transcribable_media(nothing)


def test_format_transcript_plain_branch() -> None:
    result = TranscriptionResult(plain_text="hello world")
    assert _format_transcript(result) == "hello world"


def test_format_transcript_timestamped_branch() -> None:
    result = TranscriptionResult(
        plain_text="ignored",
        sentences=(Sentence(0.0, "alpha."), Sentence(63.4, "beta.")),
    )
    out = _format_transcript(result)
    assert out == "[00:00] alpha.\n[01:03] beta."


def test_format_transcript_diarized_branch() -> None:
    result = TranscriptionResult(
        plain_text="x",
        sentences=(Sentence(0.0, "hello."), Sentence(5.0, "hi.")),
        speaker_turns=(
            SpeakerTurn(start=0.0, end=2.0, speaker=0),
            SpeakerTurn(start=2.0, end=10.0, speaker=1),
        ),
        used_diarization=True,
    )
    out = _format_transcript(result)
    assert out == "SPEAKER_00 [00:00]: hello.\nSPEAKER_01 [00:05]: hi."


# ---------------------------------------------------------------------------
# /transcribe dispatch
# ---------------------------------------------------------------------------


async def test_disabled_service_emits_guidance_message() -> None:
    handler, formatter, service = _make_handler(service_enabled=False)
    await handler.handle_transcribe(_make_ctx("/transcribe https://example.com/clip.mp3"))
    formatter.safe_reply.assert_awaited()
    # The first call carries the disabled-message text
    first_text = formatter.safe_reply.await_args_list[0].args[1]
    assert "TRANSCRIPTION_ENABLED" in first_text
    service.transcribe_media_path.assert_not_awaited()


async def test_no_url_and_no_reply_emits_usage_hint() -> None:
    handler, formatter, service = _make_handler()
    await handler.handle_transcribe(_make_ctx("/transcribe"))
    formatter.safe_reply.assert_awaited_once()
    text = formatter.safe_reply.await_args_list[0].args[1]
    assert "Usage:" in text
    service.transcribe_media_path.assert_not_awaited()


async def test_url_form_invokes_service_and_replies_with_transcript() -> None:
    handler, formatter, service = _make_handler()
    service.transcribe_media_path.return_value = TranscriptionResult(
        plain_text="recognized text",
        sentences=(Sentence(0.0, "recognized text"),),
    )

    with patch(
        "app.adapters.telegram.command_handlers.transcribe_handler.fetch_url_to_local_sync",
    ) as fetch_mock:
        from pathlib import Path

        fetch_mock.return_value = Path("/tmp/clip.mp3")
        await handler.handle_transcribe(_make_ctx("/transcribe https://example.com/clip.mp3"))

    service.transcribe_media_path.assert_awaited_once()
    # Should send at least: "Fetching audio..." + "Transcribing..." + final transcript
    assert formatter.safe_reply.await_count >= 2
    # Final reply contains the transcript text in some form
    sent_texts = [call.args[1] for call in formatter.safe_reply.await_args_list]
    assert any("recognized text" in text for text in sent_texts)


async def test_reply_form_invokes_service_when_reply_carries_voice() -> None:
    handler, formatter, service = _make_handler()
    service.transcribe_media_path.return_value = TranscriptionResult(
        plain_text="from voice",
        sentences=(Sentence(0.0, "from voice"),),
    )
    replied = MagicMock(
        voice=object(),
        audio=None,
        video_note=None,
        video=None,
        document=None,
    )
    replied.download_media = AsyncMock(return_value="/tmp/voice.ogg")

    await handler.handle_transcribe(_make_ctx("/transcribe", reply=replied))

    replied.download_media.assert_awaited_once()
    service.transcribe_media_path.assert_awaited_once()
    sent_texts = [call.args[1] for call in formatter.safe_reply.await_args_list]
    assert any("from voice" in text for text in sent_texts)


async def test_max_duration_error_relays_friendly_message() -> None:
    handler, formatter, service = _make_handler()
    service.transcribe_media_path.side_effect = TranscriptionDurationExceededError(
        duration_sec=900.0, max_duration_sec=600
    )

    with patch(
        "app.adapters.telegram.command_handlers.transcribe_handler.fetch_url_to_local_sync",
    ) as fetch_mock:
        from pathlib import Path

        fetch_mock.return_value = Path("/tmp/long.mp3")
        await handler.handle_transcribe(_make_ctx("/transcribe https://example.com/long.mp3"))

    sent_texts = [call.args[1] for call in formatter.safe_reply.await_args_list]
    assert any("max" in text.lower() and "900" in text for text in sent_texts)


async def test_transcription_disabled_during_call_surfaces_error() -> None:
    handler, formatter, service = _make_handler()
    service.transcribe_media_path.side_effect = TranscriptionDisabledError(
        "transcription is disabled"
    )

    with patch(
        "app.adapters.telegram.command_handlers.transcribe_handler.fetch_url_to_local_sync",
    ) as fetch_mock:
        from pathlib import Path

        fetch_mock.return_value = Path("/tmp/clip.mp3")
        await handler.handle_transcribe(_make_ctx("/transcribe https://example.com/clip.mp3"))

    sent_texts = [call.args[1] for call in formatter.safe_reply.await_args_list]
    assert any("disabled" in text.lower() for text in sent_texts)


@pytest.mark.parametrize("plain_text", ["", "   ", "\n\n"])
async def test_blank_transcript_emits_no_speech_message(plain_text: str) -> None:
    handler, formatter, service = _make_handler()
    service.transcribe_media_path.return_value = TranscriptionResult(plain_text=plain_text)

    with patch(
        "app.adapters.telegram.command_handlers.transcribe_handler.fetch_url_to_local_sync",
    ) as fetch_mock:
        from pathlib import Path

        fetch_mock.return_value = Path("/tmp/silent.mp3")
        await handler.handle_transcribe(_make_ctx("/transcribe https://example.com/silent.mp3"))

    sent_texts = [call.args[1] for call in formatter.safe_reply.await_args_list]
    assert any("no recognizable speech" in text.lower() for text in sent_texts)
