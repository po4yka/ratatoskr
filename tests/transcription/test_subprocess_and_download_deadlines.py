"""Every blocking call on the transcription path must be bounded.

These all run inside ``asyncio.to_thread`` on the shared default executor, so a
call that never returns does not just stall transcription: it permanently costs
one of the eight workers that the SSRF preflight, PDF extraction and summary
parsing also draw from.
"""

from __future__ import annotations

import subprocess
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from app.adapters.transcription import audio_decoder, model_resolver
from app.adapters.transcription.audio_decoder import (
    AudioDecodeError,
    decode_to_pcm,
    has_audio_stream,
    probe_duration_sec,
)


class TestFfmpegDeadlines:
    def test_every_subprocess_call_passes_a_timeout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """-nostdin rules out the classic stdin wait; a stalled mount does not."""
        seen: list[dict[str, Any]] = []

        def _fake_run(argv: list[str], **kwargs: Any) -> Any:
            seen.append(kwargs)

            class _Proc:
                stdout = "audio" if "ffprobe" in argv[0] else b""
                stderr = b""

            return _Proc()

        monkeypatch.setattr(audio_decoder.shutil, "which", lambda _name: "/usr/bin/x")
        monkeypatch.setattr(audio_decoder.subprocess, "run", _fake_run)

        media = tmp_path / "clip.mp4"
        media.write_bytes(b"x")

        has_audio_stream(media)
        probe_duration_sec(media)
        decode_to_pcm(media)

        # Four, not three: decode_to_pcm re-runs the audio-stream probe itself.
        assert len(seen) == 4
        for kwargs in seen:
            assert kwargs.get("timeout"), "a subprocess call ran with no timeout"

    def test_decode_timeout_becomes_a_domain_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """TimeoutExpired is not a CalledProcessError; unhandled it escapes raw."""
        monkeypatch.setattr(audio_decoder.shutil, "which", lambda _name: "/usr/bin/x")
        monkeypatch.setattr(audio_decoder, "has_audio_stream", lambda _p: True)

        def _timeout(argv: list[str], **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1.0)

        monkeypatch.setattr(audio_decoder.subprocess, "run", _timeout)

        media = tmp_path / "clip.mp4"
        media.write_bytes(b"x")

        with pytest.raises(AudioDecodeError, match="timed out"):
            decode_to_pcm(media)

    def test_probe_timeout_does_not_propagate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(audio_decoder.shutil, "which", lambda _name: "/usr/bin/ffprobe")

        def _timeout(argv: list[str], **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1.0)

        monkeypatch.setattr(audio_decoder.subprocess, "run", _timeout)

        media = tmp_path / "clip.mp4"
        media.write_bytes(b"x")

        # Defers to the decode step, exactly as a missing ffprobe does.
        assert has_audio_stream(media) is True
        assert probe_duration_sec(media) is None


class TestModelDownloadDeadline:
    def test_urlopen_is_given_a_timeout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """service.py holds _asr_lock across this call.

        A worker blocked here loses the lock for good and
        TranscriptionJobService stops draining its queue entirely.
        """
        captured: dict[str, Any] = {}

        def _fake_urlopen(req: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            raise urllib.error.URLError("stop here")

        monkeypatch.setattr(model_resolver.urllib.request, "urlopen", _fake_urlopen)

        with pytest.raises(model_resolver.ModelDownloadError):
            model_resolver._download("https://example.test/m.onnx", tmp_path / "m.onnx")

        assert captured.get("timeout"), "urlopen ran with no timeout"

    def test_stall_mid_body_cleans_up_the_partial_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        dest = tmp_path / "m.onnx"

        class _StallingResponse:
            def read(self, _n: int) -> bytes:
                raise TimeoutError("the CDN went quiet")

            def __enter__(self) -> _StallingResponse:
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

        monkeypatch.setattr(
            model_resolver.urllib.request, "urlopen", lambda *a, **k: _StallingResponse()
        )

        with pytest.raises(model_resolver.ModelDownloadError):
            model_resolver._download("https://example.test/m.onnx", dest)

        assert not dest.with_suffix(".onnx.part").exists(), "left a .part behind"
        assert not dest.exists()
