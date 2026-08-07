"""Hashing a voice message must not stall the loop that received it.

``audio_sha256`` read and digested the whole file inline in its coroutine. Both
halves are blocking and both scale with file size and storage latency, so on the
bot -- Telethon's single event loop -- a multi-minute voice message froze every
other user's messages for the duration. It ran on every transcription request,
not only under load.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading

from app.application.ports import transcriptions
from app.application.ports.transcriptions import audio_sha256


async def test_hash_matches_hashlib(tmp_path) -> None:
    payload = b"ratatoskr voice note" * 100_000
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(payload)

    assert await audio_sha256(audio) == hashlib.sha256(payload).hexdigest()


async def test_hash_runs_off_the_event_loop(tmp_path, monkeypatch) -> None:
    """The digest must happen on a worker thread, not the caller's loop thread."""
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"x" * 1024)

    loop_thread = threading.get_ident()
    hashing_thread: list[int] = []
    real_hash = transcriptions._sha256_of_file

    def _recording_hash(path):
        hashing_thread.append(threading.get_ident())
        return real_hash(path)

    monkeypatch.setattr(transcriptions, "_sha256_of_file", _recording_hash)

    await audio_sha256(audio)

    assert hashing_thread, "the hashing helper was never called"
    assert hashing_thread[0] != loop_thread, (
        "the file was hashed on the event-loop thread, so every other coroutine "
        "on that loop stalls for the length of the read"
    )


async def test_the_loop_keeps_running_during_a_slow_hash(tmp_path, monkeypatch) -> None:
    """A slow read must not stop other coroutines from making progress."""
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"x" * 1024)

    release = threading.Event()

    def _slow_hash(_path) -> str:
        release.wait(timeout=5)
        return "deadbeef"

    monkeypatch.setattr(transcriptions, "_sha256_of_file", _slow_hash)

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while not release.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(_ticker())
    hashing = asyncio.create_task(audio_sha256(audio))
    await asyncio.sleep(0.2)
    progressed = ticks

    release.set()
    assert await hashing == "deadbeef"
    await ticker

    assert progressed > 1, (
        f"the loop only advanced {progressed} time(s) while the file was being "
        "hashed -- the read is still blocking it"
    )
