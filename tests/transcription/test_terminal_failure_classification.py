"""A failure a retry cannot fix must not be retried.

Terminality used to be decided purely by attempt count, so a file with no audio
stream was re-downloaded, re-hashed, re-decoded and re-run through the model
three times before it was dead-lettered -- on a Raspberry Pi that shares its CPU
with Postgres, Qdrant and the bot. The exception type reached the repository only
as a string in ``error_code``, which nothing ever read.

Not everything that fails is permanent. The worker the API builds has no media
downloader, so its "cannot resolve media" errors mean *this* worker cannot do the
job, not that the job is impossible -- retrying is what routes it to the process
that can. Those stay retryable on purpose.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.adapters.transcription.audio_decoder import AudioDecodeError, NoAudioStreamError
from app.adapters.transcription.model_resolver import ModelDownloadError
from app.application.ports.transcriptions import (
    LeasedTranscriptionJob,
    PermanentTranscriptionError,
)
from app.infrastructure.persistence.repositories.transcription_repository import (
    TranscriptionRepositoryAdapter,
)


def _job(*, attempt_count: int = 1, max_attempts: int = 3) -> LeasedTranscriptionJob:
    return LeasedTranscriptionJob(
        id=1,
        user_id=42,
        source_type="telegram_voice",
        source_url=None,
        request_id=None,
        telegram_chat_id=None,
        telegram_message_id=None,
        audio_hash=None,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        correlation_id="cid-1",
    )


class TestWhichFailuresArePermanent:
    """The marker is what the failure path reads; membership is the contract."""

    @pytest.mark.parametrize("exc_cls", [AudioDecodeError, NoAudioStreamError])
    def test_undecodable_input_is_permanent(self, exc_cls: type[Exception]) -> None:
        assert issubclass(exc_cls, PermanentTranscriptionError)

    def test_duration_and_timestamp_limits_are_permanent(self) -> None:
        from app.adapters.transcription.service import (
            TimestampsUnavailableError,
            TranscriptionDurationExceededError,
        )

        assert issubclass(TranscriptionDurationExceededError, PermanentTranscriptionError)
        assert issubclass(TimestampsUnavailableError, PermanentTranscriptionError)

    def test_a_failed_model_download_stays_retryable(self) -> None:
        """It covers a network error and a timeout as well as a bad URL."""
        assert not issubclass(ModelDownloadError, PermanentTranscriptionError)

    def test_the_markers_still_behave_as_runtime_errors(self) -> None:
        """Existing `except RuntimeError` handlers must keep catching them."""
        assert issubclass(AudioDecodeError, RuntimeError)
        with pytest.raises(RuntimeError):
            raise NoAudioStreamError("no audio")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expect_terminal"),
    [
        (NoAudioStreamError("file carries no audio stream"), True),
        (AudioDecodeError("ffmpeg could not decode"), True),
        (ModelDownloadError("network error for https://models.test/x"), False),
        (RuntimeError("something transient"), False),
    ],
    ids=["no-audio", "undecodable", "model-download", "generic"],
)
async def test_the_job_service_marks_permanent_failures_terminal(
    exc: Exception, expect_terminal: bool
) -> None:
    """Drives the real failure path: the exception type must reach the repository."""
    from app.application.services.transcription_job_service import TranscriptionJobService

    recorded: dict[str, Any] = {}

    class _Repo:
        async def mark_leased_job_failed(self, job: Any, **kwargs: Any) -> str:
            recorded.update(kwargs)
            return "dead_letter" if kwargs.get("terminal") else "failed"

        async def mark_job_succeeded(self, *a: Any, **k: Any) -> None:
            return None

        async def append_progress_event(self, *a: Any, **k: Any) -> None:
            return None

    class _Transcriber:
        async def transcribe_media_path(self, *a: Any, **k: Any) -> Any:
            raise exc

    service = object.__new__(TranscriptionJobService)
    service._repo = _Repo()  # type: ignore[attr-defined]
    service._service = _Transcriber()  # type: ignore[attr-defined]
    service._cfg = SimpleNamespace(diarization_enabled=False)  # type: ignore[attr-defined]
    service._owner = "test-worker"  # type: ignore[attr-defined]
    service._retry_delay_seconds = 30  # type: ignore[attr-defined]
    service._publish = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    service._resolve_media = AsyncMock(return_value=Path("/tmp/media.ogg"))  # type: ignore[attr-defined]

    with patch(
        "app.application.services.transcription_job_service.audio_sha256",
        AsyncMock(return_value="deadbeef"),
    ):
        await service._process_leased_job(_job())

    assert recorded, "the failure path never reached the repository"
    assert recorded["terminal"] is expect_terminal


def test_the_repository_signature_accepts_terminal() -> None:
    """Guards the port/adapter contract the job service now relies on."""
    import inspect

    sig = inspect.signature(TranscriptionRepositoryAdapter.mark_leased_job_failed)
    assert "terminal" in sig.parameters
    assert sig.parameters["terminal"].default is False
