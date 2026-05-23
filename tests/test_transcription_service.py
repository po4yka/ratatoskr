"""Unit tests for TranscriptionService.

Engines are mocked at the seam so these run with no sherpa-onnx + no ffmpeg
installed. Real-binary integration coverage is intentionally deferred: it
belongs in a fixture-gated test (sherpa-onnx wheel + an ffmpeg-decodable
clip) that we do not want CI installing on every run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from app.adapters.transcription import (
    Sentence,
    TranscribeOptions,
    TranscriptionDisabledError,
    TranscriptionDurationExceededError,
    TranscriptionResult,
    TranscriptionService,
)
from app.config.transcription import TranscriptionConfig



def _enabled_cfg(**overrides: object) -> TranscriptionConfig:
    base: dict[str, object] = {
        "enabled": True,
        "model_path": Path("/tmp/transcription-model"),
        "speed": 1.5,
        "num_threads": 1,
        "max_duration_sec": 1800,
        "diarization_enabled": False,
        "diarization_model": "pyannote",
        "diarization_model_path": Path("/tmp/diarization-model"),
        "embedding_model_filename": "embedding.onnx",
        "diarization_cluster_threshold": 0.5,
        "default_num_speakers": -1,
        "auto_on_voice_message": True,
        "auto_in_url_pipeline": False,
    }
    base.update(overrides)
    return TranscriptionConfig(**base)  # type: ignore[arg-type]


async def test_disabled_service_raises_on_transcribe() -> None:
    cfg = _enabled_cfg(enabled=False)
    svc = TranscriptionService(cfg)
    assert svc.enabled is False
    with pytest.raises(TranscriptionDisabledError):
        await svc.transcribe_media_path(Path("/dev/null"))


async def test_max_duration_guard_refuses_long_media() -> None:
    cfg = _enabled_cfg(max_duration_sec=60)
    svc = TranscriptionService(cfg)

    with patch(
        "app.adapters.transcription.service.probe_duration_sec", return_value=300.0
    ):
        with pytest.raises(TranscriptionDurationExceededError) as exc_info:
            await svc.transcribe_media_path(Path("/tmp/fake.mp3"))

    assert exc_info.value.duration_sec == 300.0
    assert exc_info.value.max_duration_sec == 60


async def test_happy_path_returns_plain_text_and_sentences() -> None:
    cfg = _enabled_cfg()
    svc = TranscriptionService(cfg)

    fake_pcm = np.zeros(16000, dtype=np.float32)
    fake_sentences = (
        Sentence(start_sec=0.0, text="hello."),
        Sentence(start_sec=2.5, text="world."),
    )

    engine = MagicMock()
    engine.transcribe_sync = MagicMock(return_value=("hello. world.", fake_sentences))

    with (
        patch(
            "app.adapters.transcription.service.probe_duration_sec",
            return_value=12.0,
        ),
        patch(
            "app.adapters.transcription.service.decode_to_pcm",
            return_value=fake_pcm,
        ),
        patch.object(
            TranscriptionService, "_get_engine", AsyncMock(return_value=engine)
        ),
    ):
        result = await svc.transcribe_media_path(Path("/tmp/audio.mp3"))

    assert isinstance(result, TranscriptionResult)
    assert result.plain_text == "hello. world."
    assert result.sentences == fake_sentences
    assert result.duration_sec == 12.0
    assert result.used_diarization is False
    assert result.speaker_turns == ()


async def test_diarization_disabled_when_options_override_false() -> None:
    cfg = _enabled_cfg(diarization_enabled=True)
    svc = TranscriptionService(cfg)

    engine = MagicMock()
    engine.transcribe_sync = MagicMock(
        return_value=("text", (Sentence(start_sec=0.0, text="text"),)),
    )

    with (
        patch(
            "app.adapters.transcription.service.probe_duration_sec", return_value=5.0
        ),
        patch(
            "app.adapters.transcription.service.decode_to_pcm",
            return_value=np.zeros(8000, dtype=np.float32),
        ),
        patch.object(
            TranscriptionService, "_get_engine", AsyncMock(return_value=engine)
        ),
    ):
        result = await svc.transcribe_media_path(
            Path("/tmp/audio.mp3"),
            options=TranscribeOptions(with_diarization=False),
        )

    assert result.used_diarization is False
    assert result.speaker_turns == ()


async def test_speed_override_passed_to_engine() -> None:
    cfg = _enabled_cfg(speed=1.0)
    svc = TranscriptionService(cfg)

    engine = MagicMock()
    engine.transcribe_sync = MagicMock(return_value=("hi", (Sentence(0.0, "hi"),)))

    with (
        patch(
            "app.adapters.transcription.service.probe_duration_sec", return_value=3.0
        ),
        patch(
            "app.adapters.transcription.service.decode_to_pcm",
            return_value=np.zeros(8000, dtype=np.float32),
        ) as decode_mock,
        patch.object(
            TranscriptionService, "_get_engine", AsyncMock(return_value=engine)
        ),
    ):
        await svc.transcribe_media_path(
            Path("/tmp/audio.mp3"),
            options=TranscribeOptions(speed=2.0),
        )

    # decode_to_pcm should have been called with speed=2.0 (override), not 1.0 (cfg default)
    decode_mock.assert_called_once()
    args, _ = decode_mock.call_args
    assert args[1] == 2.0  # second positional arg is speed
    engine.transcribe_sync.assert_called_once()
    assert engine.transcribe_sync.call_args.kwargs["speed"] == 2.0


async def test_empty_recognizer_output_returns_empty_text() -> None:
    cfg = _enabled_cfg()
    svc = TranscriptionService(cfg)

    engine = MagicMock()
    # sherpa-onnx returns ("", ()) when no speech was recognized
    engine.transcribe_sync = MagicMock(return_value=("", ()))

    with (
        patch(
            "app.adapters.transcription.service.probe_duration_sec", return_value=2.0
        ),
        patch(
            "app.adapters.transcription.service.decode_to_pcm",
            return_value=np.zeros(8000, dtype=np.float32),
        ),
        patch.object(
            TranscriptionService, "_get_engine", AsyncMock(return_value=engine)
        ),
    ):
        result = await svc.transcribe_media_path(Path("/tmp/silent.mp3"))

    assert result.plain_text == ""
    assert result.sentences == ()
    assert result.used_diarization is False
