from __future__ import annotations

import asyncio

import pytest


class _WebSocket:
    def __init__(self, incoming: list[bytes]) -> None:
        self._incoming = iter(incoming)
        self.sent: list[bytes] = []

    async def receive_bytes(self) -> bytes:
        try:
            return next(self._incoming)
        except StopIteration as exc:
            raise RuntimeError("websocket closed") from exc

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)


class _Reader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)

    async def read(self, _size: int) -> bytes:
        return next(self._chunks, b"")


class _Writer:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


@pytest.mark.asyncio
async def test_binary_relay_preserves_rfb_bytes_in_both_directions() -> None:
    from app.adapters.ai_backup.vnc_gateway import relay_vnc

    websocket = _WebSocket([b"client-one", b"client-two"])
    reader = _Reader([b"server-one", b"server-two", b""])
    writer = _Writer()
    stop = asyncio.Event()

    await relay_vnc(websocket, reader, writer, stop)

    assert websocket.sent == [b"server-one", b"server-two"]
    assert writer.chunks == [b"client-one", b"client-two"]
    assert writer.closed


@pytest.mark.asyncio
async def test_binary_relay_closes_when_flow_stops() -> None:
    from app.adapters.ai_backup.vnc_gateway import relay_vnc

    class _BlockingWebSocket(_WebSocket):
        async def receive_bytes(self) -> bytes:
            await asyncio.Future()
            raise AssertionError

    class _BlockingReader(_Reader):
        async def read(self, _size: int) -> bytes:
            await asyncio.Future()
            raise AssertionError

    websocket = _BlockingWebSocket([])
    reader = _BlockingReader([])
    writer = _Writer()
    stop = asyncio.Event()
    relay = asyncio.create_task(relay_vnc(websocket, reader, writer, stop))

    stop.set()
    await asyncio.wait_for(relay, timeout=1)

    assert writer.closed


@pytest.mark.asyncio
async def test_binary_relay_awaits_sibling_cleanup_before_propagating_failure() -> None:
    from app.adapters.ai_backup.vnc_gateway import relay_vnc

    sibling_cleaned = asyncio.Event()

    class _BlockingWebSocket(_WebSocket):
        async def receive_bytes(self) -> bytes:
            try:
                await asyncio.Future()
            finally:
                sibling_cleaned.set()

    class _FailingReader(_Reader):
        async def read(self, _size: int) -> bytes:
            raise OSError("rfb read failed")

    writer = _Writer()
    with pytest.raises(OSError, match="rfb read failed"):
        await relay_vnc(_BlockingWebSocket([]), _FailingReader([]), writer, asyncio.Event())

    assert sibling_cleaned.is_set()
    assert writer.closed
