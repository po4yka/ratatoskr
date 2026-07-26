"""Bounded thread offload for memory-hard Argon2 operations.

Argon2 releases the GIL, but its Python API is synchronous and each default
credentials operation reserves 64 MiB.  Auth endpoints must therefore keep it
off the event loop without feeding an unbounded executor queue.  This module
uses both a dedicated fixed-size executor and a semaphore acquired *before*
submission, so at most ``AUTH_ARGON2_MAX_CONCURRENCY`` calls are running or
queued in the executor per API process.
"""

from __future__ import annotations

import asyncio
import functools
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from app.config import load_config

if TYPE_CHECKING:
    from collections.abc import Callable

P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class _Argon2OffloadState:
    executor_lock: threading.Lock = field(default_factory=threading.Lock)
    executor: ThreadPoolExecutor | None = None
    executor_limit: int | None = None
    gate_lock: threading.Lock = field(default_factory=threading.Lock)
    loop_gates: weakref.WeakKeyDictionary[
        asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]
    ] = field(default_factory=weakref.WeakKeyDictionary)


_state = _Argon2OffloadState()


def _get_max_concurrency() -> int:
    return load_config(allow_stub_telegram=True).auth.argon2_max_concurrency


def _get_executor(limit: int) -> ThreadPoolExecutor:
    """Return the process-wide executor, rebuilding it after a config reload."""
    with _state.executor_lock:
        if _state.executor is None or _state.executor_limit != limit:
            previous = _state.executor
            _state.executor = ThreadPoolExecutor(
                max_workers=limit,
                thread_name_prefix="ratatoskr-argon2",
            )
            _state.executor_limit = limit
            if previous is not None:
                # Runtime config is immutable in production.  This branch is
                # useful for tests/config reloads; already-running work is not
                # cancelled and its executor exits after that work completes.
                previous.shutdown(wait=False, cancel_futures=False)
        return _state.executor


def _get_gate(loop: asyncio.AbstractEventLoop, limit: int) -> asyncio.Semaphore:
    """Return an event-loop-local gate backed by the process-wide limit."""
    with _state.gate_lock:
        current = _state.loop_gates.get(loop)
        if current is None or current[0] != limit:
            gate = asyncio.Semaphore(limit)
            _state.loop_gates[loop] = (limit, gate)
            return gate
        return current[1]


async def _await_worker(future: asyncio.Future[R]) -> R:
    """Defer task cancellation until the worker has released its RAM.

    ``run_in_executor`` cannot stop a running C function.  Releasing the
    semaphore immediately on HTTP disconnect would therefore allow replacement
    Argon2 calls to start while cancelled calls still consume memory.  Shield
    the worker and remember cancellation until its result has been collected.
    """
    cancelled: asyncio.CancelledError | None = None
    while not future.done():
        # nosemgrep: broad-except-base - preserve worker failure until cleanup
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except BaseException:
            break

    # nosemgrep: broad-except-base - re-raise after cancellation handling
    try:
        result = future.result()
    except BaseException:
        if cancelled is not None:
            raise cancelled from None
        raise
    if cancelled is not None:
        raise cancelled
    return result


async def run_argon2(func: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run one synchronous Argon2 operation without blocking the event loop."""
    limit = _get_max_concurrency()
    loop = asyncio.get_running_loop()
    gate = _get_gate(loop, limit)

    # Acquire before submission: ThreadPoolExecutor's internal work queue is
    # unbounded, so max_workers alone is not a sufficient admission bound.
    async with gate:
        executor = _get_executor(limit)
        call = functools.partial(func, *args, **kwargs)
        future = loop.run_in_executor(executor, call)
        return await _await_worker(future)


def _reset_for_tests() -> None:
    """Release executor/gate state between tests that change the limit."""
    with _state.executor_lock:
        previous = _state.executor
        _state.executor = None
        _state.executor_limit = None
    if previous is not None:
        previous.shutdown(wait=True, cancel_futures=True)
    with _state.gate_lock:
        _state.loop_gates.clear()
