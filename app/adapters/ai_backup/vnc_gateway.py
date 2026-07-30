"""Opaque WebSocket-to-RFB relay for an internal VNC target.

The gateway deliberately knows nothing about RFB messages. It preserves bytes,
applies asyncio stream backpressure, and closes both directions together.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class VncTarget:
    host: str
    port: int


class VncConnector(Protocol):
    async def connect(
        self, target: VncTarget, timeout_seconds: float
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]: ...


class TcpVncConnector:
    """Production adapter opening a bounded TCP connection inside Docker."""

    async def connect(
        self, target: VncTarget, timeout_seconds: float
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        async with asyncio.timeout(timeout_seconds):
            return await asyncio.open_connection(target.host, target.port)


async def relay_vnc(
    websocket: Any,
    reader: Any,
    writer: Any,
    stop_event: asyncio.Event,
) -> None:
    """Relay raw binary RFB bytes until either peer or the owning flow closes."""

    async def websocket_to_vnc() -> None:
        try:
            while True:
                payload = await websocket.receive_bytes()
                writer.write(payload)
                await writer.drain()
        except Exception:
            # A WebSocket peer going away is a normal tunnel terminator. The
            # API layer owns protocol close codes; this module owns raw bytes.
            return

    async def vnc_to_websocket() -> None:
        while payload := await reader.read(64 * 1024):
            await websocket.send_bytes(payload)

    async def wait_for_stop() -> None:
        await stop_event.wait()

    tasks = {
        asyncio.create_task(websocket_to_vnc(), name="rfb:websocket-to-vnc"),
        asyncio.create_task(vnc_to_websocket(), name="rfb:vnc-to-websocket"),
        asyncio.create_task(wait_for_stop(), name="rfb:flow-stop"),
    }
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        await writer.wait_closed()
