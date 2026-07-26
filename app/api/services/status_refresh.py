"""Lifecycle-owned refresh loop for continuously current public status metrics."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.api.services.status_service import PublicStatusService
    from app.config.deployment import DeploymentConfig

logger = get_logger(__name__)


def status_refresh_interval_seconds(deployment: DeploymentConfig) -> float:
    """Refresh often enough that one cache expiry is observed within its TTL."""
    return max(
        1.0,
        min(
            deployment.status_cache_ttl_seconds,
            deployment.status_refresh_after_seconds,
        )
        / 2,
    )


async def run_public_status_refresh_loop(
    service: PublicStatusService,
    *,
    interval_seconds: float,
    timeout_seconds: float,
) -> None:
    """Continuously evaluate status with bounded work and cancellation safety."""
    while True:
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_seconds):
                await service.get_status()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning("public_status_background_refresh_timed_out")
        except Exception as exc:
            logger.warning(
                "public_status_background_refresh_failed",
                extra={"error_type": type(exc).__name__},
            )
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(0.1, interval_seconds - elapsed))
