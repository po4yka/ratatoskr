"""Tests for deriving credential->config mappings and the refresh loop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import AliasChoices, BaseModel, Field

from app.config.credential_reloader import _alias_names, start_credential_refresh_task


class _Section(BaseModel):
    api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    multi: str = Field(
        default="", validation_alias=AliasChoices("TRANSCRIPTION_API_KEY", "STT_API_KEY")
    )


def test_alias_names_handles_plain_string() -> None:
    assert _alias_names("OPENROUTER_API_KEY") == ["OPENROUTER_API_KEY"]


def test_alias_names_handles_alias_choices() -> None:
    """A field reachable under several env names must map under all of them.

    The transcription section does exactly this, and treating the alias as a
    bare string silently dropped it from the mapping.
    """
    field = _Section.model_fields["multi"]
    assert _alias_names(field.validation_alias) == ["TRANSCRIPTION_API_KEY", "STT_API_KEY"]


def test_alias_names_ignores_unset_alias() -> None:
    assert _alias_names(None) == []


class _StubReloaderStore:
    """Minimal CredentialStore stand-in that counts resolve calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, key: str, *, user_id: int) -> str | None:
        del key, user_id
        self.calls += 1
        return None


class _StubHolder:
    def __init__(self) -> None:
        self._cfg = SimpleNamespace()
        self.swaps = 0

    @property
    def cfg(self) -> Any:
        return self._cfg

    def swap(self, new_cfg: Any) -> Any:
        del new_cfg
        self.swaps += 1


@pytest.mark.asyncio
async def test_refresh_task_survives_a_failing_tick() -> None:
    """One bad poll must not kill the loop -- the next tick has to still run."""
    holder = _StubHolder()
    calls: list[int] = []

    class _Boom:
        async def resolve(self, key: str, *, user_id: int) -> str | None:
            del key, user_id
            return None

    from app.config import credential_reloader as mod

    original = mod.CredentialConfigReloader.refresh

    async def _flaky(self: Any) -> bool:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return False

    mod.CredentialConfigReloader.refresh = _flaky  # type: ignore[method-assign]
    try:
        task = start_credential_refresh_task(
            holder,  # type: ignore[arg-type]
            _Boom(),  # type: ignore[arg-type]
            owner_id=1,
            interval_sec=0.01,
        )
        await asyncio.sleep(0.08)
        assert len(calls) >= 2, "loop stopped after the failing tick"
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        mod.CredentialConfigReloader.refresh = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_refresh_task_cancels_cleanly() -> None:
    task = start_credential_refresh_task(
        _StubHolder(),  # type: ignore[arg-type]
        _StubReloaderStore(),  # type: ignore[arg-type]
        owner_id=1,
        interval_sec=10.0,
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
