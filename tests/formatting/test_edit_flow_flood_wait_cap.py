"""A long FloodWait must abandon the edit, not sit on it.

Telethon already sleeps FloodWaits below its own flood_sleep_threshold (60 s by
default), so every wait that reaches this retry loop is a long one. Obeying it
verbatim -- and the loop allows two retries -- held the caller's concurrency slot
and typing task for the better part of an hour, and delayed the fresh-send
fallback by exactly as long.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.adapters.external.formatting._response_sender_edit_flow import (
    _EDIT_MAX_FLOOD_WAIT_SEC,
    ResponseSenderEditFlow,
)


def mod_ref():
    import app.adapters.external.formatting._response_sender_edit_flow as mod

    return mod


class _FloodWait(Exception):
    """Shaped like telethon's FloodWaitError, which carries `seconds`."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"A wait of {seconds} seconds is required")
        self.seconds = seconds


def _flow(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> tuple[Any, list[float]]:
    flow = object.__new__(ResponseSenderEditFlow)
    # edit_message runs validate_and_truncate and a rate-limit check before the
    # retry loop; neither is what this test is about.
    flow._state = SimpleNamespace(
        validator=SimpleNamespace(check_rate_limit=AsyncMock(return_value=True))
    )
    monkeypatch.setattr(mod_ref(), "validate_and_truncate", lambda _state, text, **_k: text)
    monkeypatch.setattr(flow, "_log_edit_attempt", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(flow, "_resolve_edit_client", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(flow, "_validate_edit_identifiers", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(flow, "_perform_edit_message", AsyncMock(side_effect=exc), raising=False)

    slept: list[float] = []

    async def _sleep(delay: float) -> None:
        slept.append(delay)

    import app.adapters.external.formatting._response_sender_edit_flow as mod

    monkeypatch.setattr(mod.asyncio, "sleep", _sleep)
    return flow, slept


@pytest.mark.asyncio
async def test_long_flood_wait_is_refused_rather_than_slept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow, slept = _flow(monkeypatch, _FloodWait(seconds=1800))

    ok = await flow.edit_message(1, 2, "text")

    assert ok is False
    assert slept == [], f"slept on a {1800}s FloodWait: {slept}"


@pytest.mark.asyncio
async def test_short_flood_wait_is_still_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below the cap the server's number is still obeyed."""
    wait = _EDIT_MAX_FLOOD_WAIT_SEC / 2
    flow, slept = _flow(monkeypatch, _FloodWait(seconds=wait))

    ok = await flow.edit_message(1, 2, "text")

    assert ok is False  # all retries exhausted
    assert slept and all(s == wait for s in slept)
