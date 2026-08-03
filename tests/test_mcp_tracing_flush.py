"""The MCP process must drain its span buffer before the container goes away.

``run_server`` calls ``init_tracing`` and, until this was added, nothing ever
called ``shutdown_tracing``. All three deployed MCP services run
``MCP_TRANSPORT=sse``, so the process is uvicorn, and uvicorn is where the
usual escape hatches stop working.

Measured on starlette 1.3.1 / uvicorn 0.51 by SIGTERMing a real server:

  * the ASGI lifespan shutdown runs
  * a ``finally`` after ``uvicorn.run(...)`` does not -- ``capture_signals()``
    restores SIG_DFL and re-raises the signal before that call returns, so the
    process dies inside it (exit 143)
  * ``atexit`` does not run either, which is what
    ``TracerProvider(shutdown_on_exit=True)`` relies on and the only thing that
    had ever flushed this process

Everything the BatchSpanProcessor still held -- queue up to 2048 spans, 5 s
scheduling delay -- was therefore dropped on every restart. The lifespan is the
one seam left, so these tests drive the lifespan protocol itself rather than
asserting that some function was wired somewhere.

The flush is also the one thing standing between SIGTERM and the process exit,
so it is bounded; the last test is the one that says so.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from starlette.applications import Starlette

from app.mcp.server import _build_sse_app


class _StubMcp:
    """Stands in for FastMCP. ``sse_app`` returns what the real one returns.

    Pinned by ``test_the_sdk_still_returns_an_app_with_a_router`` below, so this
    double cannot drift away from the SDK unnoticed.
    """

    def __init__(self, app: Starlette) -> None:
        self._app = app

    def sse_app(self) -> Starlette:
        return self._app


def _sse_app(app: Starlette, *, auth_mode: str = "disabled") -> Any:
    return _build_sse_app(
        mcp_server=_StubMcp(app),  # type: ignore[arg-type]
        auth_mode=auth_mode,
        forwarded_access_token_header="X-Ratatoskr-Forwarded-Access-Token",
        forwarded_secret_header="X-Ratatoskr-MCP-Forwarding-Secret",
        forwarding_secret="secret",
    )


async def _run_lifespan(
    asgi_app: Any, log: list[str], *, throw: BaseException | None = None
) -> dict[str, Any]:
    """Speak the ASGI lifespan protocol to *asgi_app*; return the scope.

    ``throw`` raises out of the second ``receive()`` instead of delivering
    ``lifespan.shutdown``, which is how a stop reaches the lifespan when the
    server cancels its task rather than sending it a message.
    """
    incoming: list[dict[str, str]] = [{"type": "lifespan.startup"}]
    if throw is None:
        incoming.append({"type": "lifespan.shutdown"})

    async def receive() -> dict[str, str]:
        if not incoming:
            assert throw is not None
            raise throw
        return incoming.pop(0)

    async def send(message: dict[str, Any]) -> None:
        log.append(message["type"])

    scope: dict[str, Any] = {"type": "lifespan", "app": asgi_app, "state": {}}
    await asgi_app(scope, receive, send)
    return scope


def _tracing_log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """One ordered log for both lifespan messages and the flush."""
    log: list[str] = []
    monkeypatch.setattr("app.observability.otel.shutdown_tracing", lambda: log.append("flush"))
    return log


@pytest.mark.asyncio
async def test_the_flush_happens_at_shutdown_and_not_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order is the whole assertion.

    After startup.complete, so a boot-time flush cannot shut the provider down
    for the rest of the run; before shutdown.complete, so uvicorn is still
    waiting on us and the export is not racing the process exit.
    """
    log = _tracing_log(monkeypatch)

    await _run_lifespan(_sse_app(Starlette()), log)

    assert log == ["lifespan.startup.complete", "flush", "lifespan.shutdown.complete"]


@pytest.mark.asyncio
async def test_the_flush_survives_the_jwt_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """mcp-public wraps the app in McpHttpAuthMiddleware, a raw ASGI callable."""
    log = _tracing_log(monkeypatch)

    await _run_lifespan(_sse_app(Starlette(), auth_mode="jwt"), log)

    assert log == ["lifespan.startup.complete", "flush", "lifespan.shutdown.complete"]


@pytest.mark.asyncio
async def test_an_sdk_lifespan_is_wrapped_not_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK owns that object; today it is a no-op, tomorrow it may not be."""
    log = _tracing_log(monkeypatch)

    @contextlib.asynccontextmanager
    async def sdk_lifespan(_app: Any) -> AsyncGenerator[dict[str, str]]:
        log.append("sdk-start")
        yield {"sdk": "state"}
        log.append("sdk-stop")

    scope = await _run_lifespan(_sse_app(Starlette(lifespan=sdk_lifespan)), log)

    # Flush last, so anything the SDK emits while tearing down still goes out.
    assert log == [
        "sdk-start",
        "lifespan.startup.complete",
        "sdk-stop",
        "flush",
        "lifespan.shutdown.complete",
    ]
    # The state the SDK yielded has to survive the wrap. Starlette only copies it
    # when it is not None, so dropping it is silent -- no error, no failing test,
    # just an app whose lifespan state vanished.
    assert scope["state"] == {"sdk": "state"}


@pytest.mark.asyncio
async def test_a_cancelled_lifespan_still_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case the ``finally`` exists for.

    A server that stops by cancelling the lifespan task -- rather than sending
    ``lifespan.shutdown`` -- throws into this generator while it is suspended at
    the yield. Without the ``finally`` the flush is simply never reached, and no
    other test here notices: they all deliver the message and resume normally.
    """
    log = _tracing_log(monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        await _run_lifespan(_sse_app(Starlette()), log, throw=asyncio.CancelledError())

    assert log == ["lifespan.startup.complete", "flush", "lifespan.shutdown.failed"]


@pytest.mark.asyncio
async def test_a_failing_sdk_startup_still_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tracing is already running by then, so those spans are worth the same."""
    log = _tracing_log(monkeypatch)

    @contextlib.asynccontextmanager
    async def broken(_app: Any) -> AsyncGenerator[None]:
        raise RuntimeError("startup blew up")
        yield  # pragma: no cover - unreachable, but it must stay a generator

    with pytest.raises(RuntimeError, match="startup blew up"):
        await _run_lifespan(_sse_app(Starlette(lifespan=broken)), log)

    assert log == ["flush", "lifespan.startup.failed"]


@pytest.mark.asyncio
async def test_a_stalled_exporter_cannot_hold_the_container_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flush is bounded, because the thing it talks to is usually not there.

    ``shutdown_tracing`` blocks: BatchSpanProcessor joins its worker thread for
    up to 30 s, and uvicorn waits on this lifespan with no timeout of its own.
    Every MCP service sets ``stop_grace_period: 30s``, so an unbounded flush
    against an unreachable collector would spend the entire budget and then be
    SIGKILLed with the spans still buffered -- a slower stop and no telemetry.

    The assertion is that the lifespan came back while the exporter was still
    stuck -- not that it came back quickly. A wall-clock threshold said the same
    thing but failed once under a loaded full suite, and a flaky guard is one
    nobody trusts the next time it goes red.
    """
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def stalls() -> None:
        started.set()
        release.wait(10)  # released below, so the worker thread is not leaked
        finished.set()

    monkeypatch.setattr("app.observability.otel.shutdown_tracing", stalls)
    monkeypatch.setattr("app.mcp.server._SPAN_FLUSH_TIMEOUT_SEC", 0.05)
    log: list[str] = []

    try:
        await _run_lifespan(_sse_app(Starlette()), log)
        assert not finished.is_set(), "the shutdown sat and waited for the exporter"
    finally:
        release.set()

    assert log == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    # Generous on purpose: a busy executor may only get to the thread now, and
    # that is still the bounded behaviour working. Nothing running at all is not.
    assert started.wait(10), "the flush was never even attempted"


def test_the_sdk_still_returns_an_app_with_a_router() -> None:
    """The hook assumes ``sse_app()`` hands back something with a lifespan.

    If a future MCP SDK returns a bare ASGI callable instead, the flush would
    raise at startup rather than silently stop happening -- but this says so
    here, at the version bump, instead of on the Pi.
    """
    from mcp.server.fastmcp import FastMCP

    app = FastMCP("contract-probe").sse_app()

    assert hasattr(app, "router")
    assert hasattr(app.router, "lifespan_context")
