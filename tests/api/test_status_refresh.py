from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.api.services.status_refresh import (
    run_public_status_refresh_loop,
    status_refresh_interval_seconds,
)
from app.config.deployment import DeploymentConfig


def test_refresh_interval_is_within_cache_and_client_refresh_windows() -> None:
    deployment = DeploymentConfig(
        STATUS_CACHE_TTL_SECONDS=20,
        STATUS_REFRESH_AFTER_SECONDS=30,
    )

    interval = status_refresh_interval_seconds(deployment)

    assert interval == 10
    assert interval <= deployment.status_cache_ttl_seconds
    assert interval <= deployment.status_refresh_after_seconds


@pytest.mark.asyncio
async def test_refresh_loop_evaluates_immediately_and_cancels_cleanly() -> None:
    refreshed = asyncio.Event()
    service = SimpleNamespace()

    async def _get_status() -> None:
        refreshed.set()

    service.get_status = _get_status
    task = asyncio.create_task(
        run_public_status_refresh_loop(
            service,
            interval_seconds=60,
            timeout_seconds=1,
        )
    )

    await asyncio.wait_for(refreshed.wait(), timeout=0.5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_refresh_loop_bounds_stuck_evaluation_and_continues() -> None:
    second_refresh = asyncio.Event()
    calls = 0
    service = SimpleNamespace()

    async def _get_status() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.Event().wait()
        second_refresh.set()

    service.get_status = _get_status
    task = asyncio.create_task(
        run_public_status_refresh_loop(
            service,
            interval_seconds=0.01,
            timeout_seconds=0.01,
        )
    )
    try:
        await asyncio.wait_for(second_refresh.wait(), timeout=0.5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert calls >= 2
