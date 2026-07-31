"""The sherpa-onnx recognizer must load exactly once under concurrent transcriptions.

``service.py`` runs ``engine.transcribe_sync`` via ``asyncio.to_thread``, and the
recognizer is built lazily inside that call. Nothing upstream serialises
transcription: ``/transcribe``, the voice-message processor, the YouTube
download pipeline and ``TranscriptionJobService`` all share one cached engine and
can call it at the same time. Two simultaneous loads would hold two copies of a
~230 MB model in RAM -- fatal on the Pi.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.adapters.transcription.asr_engine import OfflineAsrEngine, StreamingAsrEngine


class _FakeStream:
    result = None

    def accept_waveform(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def input_finished(self) -> None:
        return None


def _make_fake_sherpa(loads: list[float], barrier_sec: float) -> types.ModuleType:
    class _FakeRecognizer:
        def __init__(self) -> None:
            loads.append(time.monotonic())
            # Widen the window a real ONNX session build leaves open.
            time.sleep(barrier_sec)

        @classmethod
        def from_transducer(cls, **_kwargs: Any) -> _FakeRecognizer:
            return cls()

        def create_stream(self) -> _FakeStream:
            return _FakeStream()

        def is_ready(self, _stream: _FakeStream) -> bool:
            return False

        def get_result(self, _stream: _FakeStream) -> str:
            return "ok"

        def decode_stream(self, _stream: _FakeStream) -> None:
            return None

    module = types.ModuleType("sherpa_onnx")
    module.OnlineRecognizer = _FakeRecognizer  # type: ignore[attr-defined]
    module.OfflineRecognizer = _FakeRecognizer  # type: ignore[attr-defined]
    return module


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    for name in ("encoder.onnx", "decoder.onnx", "joiner.onnx"):
        (tmp_path / name).write_bytes(b"\x00" * 2048)
    (tmp_path / "tokens.txt").write_text("a 0\n")
    return tmp_path


@pytest.mark.parametrize("engine_cls", [StreamingAsrEngine, OfflineAsrEngine])
@pytest.mark.asyncio
async def test_concurrent_transcriptions_load_the_model_once(
    engine_cls: type[StreamingAsrEngine] | type[OfflineAsrEngine],
    model_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[float] = []
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _make_fake_sherpa(loads, barrier_sec=0.3))

    # One shared engine object -- exactly what the process-wide _SERVICE_CACHE
    # hands to every caller.
    engine = engine_cls(model_dir=model_dir, num_threads=2)
    samples = np.zeros(16000, dtype=np.float32)

    await asyncio.gather(
        *(asyncio.to_thread(engine.transcribe_sync, samples, speed=1.0) for _ in range(4)),
    )

    assert len(loads) == 1, f"{len(loads)} concurrent sherpa-onnx model loads; expected 1"


@pytest.mark.asyncio
async def test_a_failed_load_does_not_wedge_the_lock(
    model_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising load must release the lock so the next attempt can retry."""
    attempts: list[int] = []

    class _Boom:
        @staticmethod
        def from_transducer(**_kwargs: Any) -> Any:
            attempts.append(1)
            msg = "onnx load failed"
            raise RuntimeError(msg)

    module = types.ModuleType("sherpa_onnx")
    module.OnlineRecognizer = _Boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sherpa_onnx", module)

    engine = StreamingAsrEngine(model_dir=model_dir, num_threads=1)
    samples = np.zeros(16000, dtype=np.float32)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await asyncio.to_thread(engine.transcribe_sync, samples, speed=1.0)

    assert len(attempts) == 2, "the second call never retried -- the lock is not reentrant-safe"
    assert not engine._load_lock.locked(), "load lock stayed held after the failure"


def test_load_lock_is_a_thread_lock() -> None:
    """asyncio.Lock cannot guard a to_thread worker; the lock must be a thread lock."""
    engine = StreamingAsrEngine(model_dir=Path("/nonexistent"), num_threads=1)
    assert isinstance(engine._load_lock, type(threading.Lock()))
