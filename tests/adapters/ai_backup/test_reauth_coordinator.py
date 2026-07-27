from __future__ import annotations

import asyncio
import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.ai_backup import (
    AiBackupAuthorizationStatus,
    AiBackupService,
    AiBackupStatus,
)
from app.security.secret_crypto import InvalidEncryptedSecretError


class _Mouse:
    def __init__(self) -> None:
        self.click = AsyncMock()
        self.move = AsyncMock()
        self.wheel = AsyncMock()


class _Keyboard:
    def __init__(self) -> None:
        self.press = AsyncMock()
        self.insert_text = AsyncMock()


class _Page:
    def __init__(self) -> None:
        self.url = "https://chatgpt.com/"
        self.goto = AsyncMock()
        self.screenshot = AsyncMock(return_value=b"jpeg-frame")
        self.mouse = _Mouse()
        self.keyboard = _Keyboard()


class _Context:
    def __init__(self, state: dict) -> None:
        self._state = state

    async def storage_state(self) -> dict:
        return self._state


def _cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.scraper.cloakbrowser_url = "http://cloakbrowser:9222"
    cfg.scraper.cloakbrowser_proxy = ""
    cfg.ai_backup.enabled = True
    cfg.ai_backup.chatgpt_enabled = True
    cfg.ai_backup.claude_enabled = True
    return cfg


async def _wait_for_state(coordinator: object, flow_id: str, state: str) -> object:
    for _ in range(100):
        snapshot = await coordinator.get(42, flow_id)  # type: ignore[attr-defined]
        if snapshot.state.value == state:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError(f"flow {flow_id} did not reach {state}")


@pytest.mark.asyncio
async def test_reauth_captures_session_enqueues_targeted_backup_and_completes() -> None:
    from app.adapters.ai_backup.reauth import AiBackupReauthCoordinator

    page = _Page()
    state = {
        "cookies": [
            {
                "name": "__Secure-next-auth.session-token",
                "domain": ".chatgpt.com",
                "value": "fresh-session",
            }
        ],
        "origins": [],
    }

    @asynccontextmanager
    async def browser_context(*_args: object, **_kwargs: object):
        yield page, _Context(state)

    store = MagicMock()
    store.load = AsyncMock(return_value=None)
    store.save = AsyncMock()
    repo = MagicMock()
    repo.mark_authorization_unverified = AsyncMock()
    repo.get = AsyncMock(
        side_effect=lambda *_args: SimpleNamespace(
            status=AiBackupStatus.OK,
            authorization_status=AiBackupAuthorizationStatus.VALID,
            last_attempt_at=dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=1),
            last_error=None,
        )
    )
    enqueue = AsyncMock()
    probe = AsyncMock(return_value=True)
    coordinator = AiBackupReauthCoordinator(
        cfg=_cfg(),
        db=MagicMock(),
        session_store=store,
        repository=repo,
        browser_context_factory=browser_context,
        auth_probe=probe,
        enqueue_backup=enqueue,
        poll_interval_seconds=0.01,
        flow_timeout_seconds=2,
    )

    started = await coordinator.start(42, AiBackupService.CHATGPT)
    completed = await _wait_for_state(coordinator, started.id, "completed")

    assert completed.service == AiBackupService.CHATGPT
    store.save.assert_awaited_once_with(42, AiBackupService.CHATGPT, state)
    repo.mark_authorization_unverified.assert_awaited_once_with(42, AiBackupService.CHATGPT)
    enqueue.assert_awaited_once_with(42, AiBackupService.CHATGPT)
    await coordinator.close()


@pytest.mark.asyncio
async def test_waiting_flow_proxies_frame_pointer_and_keyboard_without_exposing_cdp() -> None:
    from app.adapters.ai_backup.reauth import AiBackupReauthCoordinator, ReauthInputEvent

    page = _Page()
    state = {"cookies": [{"name": "sessionKey", "domain": ".claude.ai", "value": "fresh-session"}]}

    @asynccontextmanager
    async def browser_context(*_args: object, **_kwargs: object):
        yield page, _Context(state)

    coordinator = AiBackupReauthCoordinator(
        cfg=_cfg(),
        db=MagicMock(),
        session_store=MagicMock(load=AsyncMock(return_value=None)),
        repository=MagicMock(),
        browser_context_factory=browser_context,
        auth_probe=AsyncMock(return_value=False),
        enqueue_backup=AsyncMock(),
        poll_interval_seconds=0.01,
        flow_timeout_seconds=2,
    )

    started = await coordinator.start(42, AiBackupService.CLAUDE)
    await _wait_for_state(coordinator, started.id, "waiting_for_user")

    assert await coordinator.capture_frame(42, started.id) == b"jpeg-frame"
    await coordinator.send_input(42, started.id, ReauthInputEvent(type="click", x=120, y=80))
    await coordinator.send_input(42, started.id, ReauthInputEvent(type="text", text="not-logged"))
    await coordinator.send_input(42, started.id, ReauthInputEvent(type="key", key="Enter"))

    page.mouse.click.assert_awaited_once_with(120, 80)
    page.keyboard.insert_text.assert_awaited_once_with("not-logged")
    page.keyboard.press.assert_awaited_once_with("Enter")
    await coordinator.cancel(42, started.id)
    await coordinator.close()


@pytest.mark.asyncio
async def test_flow_is_owner_scoped_and_service_flags_are_enforced() -> None:
    from app.adapters.ai_backup.reauth import AiBackupReauthCoordinator

    cfg = _cfg()
    cfg.ai_backup.claude_enabled = False
    coordinator = AiBackupReauthCoordinator(
        cfg=cfg,
        db=MagicMock(),
        session_store=MagicMock(load=AsyncMock(return_value=None)),
        repository=MagicMock(),
        auth_probe=AsyncMock(return_value=False),
        enqueue_backup=AsyncMock(),
    )

    with pytest.raises(ValueError, match="disabled"):
        await coordinator.start(42, AiBackupService.CLAUDE)

    started = await coordinator.start(42, AiBackupService.CHATGPT)
    with pytest.raises(KeyError):
        await coordinator.get(99, started.id)
    await coordinator.close()


@pytest.mark.asyncio
async def test_invalid_saved_session_opens_clean_recovery_browser() -> None:
    from app.adapters.ai_backup.reauth import AiBackupReauthCoordinator

    page = _Page()
    received_states: list[dict | None] = []

    @asynccontextmanager
    async def browser_context(_domain: str, storage_state: dict | None, **_kwargs: object):
        received_states.append(storage_state)
        yield page, _Context({"cookies": []})

    store = MagicMock()
    store.load = AsyncMock(side_effect=InvalidEncryptedSecretError("old key"))
    coordinator = AiBackupReauthCoordinator(
        cfg=_cfg(),
        db=MagicMock(),
        session_store=store,
        repository=MagicMock(),
        browser_context_factory=browser_context,
        auth_probe=AsyncMock(return_value=False),
        enqueue_backup=AsyncMock(),
        poll_interval_seconds=0.01,
        flow_timeout_seconds=2,
    )

    started = await coordinator.start(42, AiBackupService.CHATGPT)
    await _wait_for_state(coordinator, started.id, "waiting_for_user")

    assert received_states == [None]
    await coordinator.cancel(42, started.id)
    await coordinator.close()


@pytest.mark.asyncio
async def test_frame_rechecks_readiness_after_waiting_for_browser_lock() -> None:
    from app.adapters.ai_backup.reauth import AiBackupReauthCoordinator, ReauthFlowState

    page = _Page()

    @asynccontextmanager
    async def browser_context(*_args: object, **_kwargs: object):
        yield page, _Context({"cookies": []})

    coordinator = AiBackupReauthCoordinator(
        cfg=_cfg(),
        db=MagicMock(),
        session_store=MagicMock(load=AsyncMock(return_value=None)),
        repository=MagicMock(),
        browser_context_factory=browser_context,
        auth_probe=AsyncMock(return_value=False),
        enqueue_backup=AsyncMock(),
        poll_interval_seconds=0.01,
        flow_timeout_seconds=2,
    )
    started = await coordinator.start(42, AiBackupService.CHATGPT)
    await _wait_for_state(coordinator, started.id, "waiting_for_user")
    flow = coordinator._flows[started.id]
    await flow.browser_lock.acquire()
    capture = asyncio.create_task(coordinator.capture_frame(42, started.id))
    await asyncio.sleep(0)
    flow.state = ReauthFlowState.VERIFYING
    flow.page = None
    flow.browser_lock.release()

    with pytest.raises(RuntimeError, match="not ready"):
        await capture
    await coordinator.cancel(42, started.id)
    await coordinator.close()
