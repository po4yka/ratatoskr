"""Short-lived, owner-scoped interactive re-authorization for AI backups.

The coordinator exposes a browser as screenshots plus bounded input events.  It
never publishes CloakBrowser's unauthenticated CDP endpoint and never persists
credentials typed during login.  Only the resulting Playwright storage state is
saved, through the existing encrypted ``AiBackupSessionStore``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import enum
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from app.core.logging_utils import get_logger
from app.db.models.ai_backup import (
    AiBackupAuthorizationStatus,
    AiBackupService,
    AiBackupStatus,
)

if TYPE_CHECKING:
    from app.config import AppConfig
    from app.db.session import Database

logger = get_logger(__name__)

_LOGIN_URLS = {
    AiBackupService.CHATGPT: "https://chatgpt.com/",
    AiBackupService.CLAUDE: "https://claude.ai/",
}
_ACTIVE_STATES = frozenset({"starting", "waiting_for_user", "verifying", "resuming_backup"})
_TERMINAL_STATES = frozenset({"completed", "failed", "expired", "cancelled"})


class ReauthFlowState(enum.StrEnum):
    STARTING = "starting"
    WAITING_FOR_USER = "waiting_for_user"
    VERIFYING = "verifying"
    RESUMING_BACKUP = "resuming_backup"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ReauthInputEvent:
    type: Literal["click", "move", "wheel", "key", "text"]
    x: float | None = None
    y: float | None = None
    delta_x: float | None = None
    delta_y: float | None = None
    key: str | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class ReauthFlowSnapshot:
    id: str
    service: AiBackupService
    state: ReauthFlowState
    created_at: dt.datetime
    expires_at: dt.datetime
    error: str | None = None


@dataclass(slots=True)
class _Flow:
    id: str
    user_id: int
    service: AiBackupService
    state: ReauthFlowState
    created_at: dt.datetime
    expires_at: dt.datetime
    error: str | None = None
    page: Any | None = None
    context: Any | None = None
    task: asyncio.Task[None] | None = None
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    browser_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def snapshot(self) -> ReauthFlowSnapshot:
        return ReauthFlowSnapshot(
            id=self.id,
            service=self.service,
            state=self.state,
            created_at=self.created_at,
            expires_at=self.expires_at,
            error=self.error,
        )


BrowserContextFactory = Callable[..., AbstractAsyncContextManager[tuple[Any, Any]]]
AuthProbe = Callable[[Any, Any, AiBackupService], Awaitable[bool]]
EnqueueBackup = Callable[[int, AiBackupService], Awaitable[None]]


class AiBackupReauthCoordinator:
    """Own the lifecycle of ephemeral interactive login browser contexts."""

    def __init__(
        self,
        *,
        cfg: AppConfig,
        db: Database,
        session_store: Any | None = None,
        repository: Any | None = None,
        browser_context_factory: BrowserContextFactory | None = None,
        auth_probe: AuthProbe | None = None,
        enqueue_backup: EnqueueBackup | None = None,
        poll_interval_seconds: float = 1.0,
        flow_timeout_seconds: float = 15 * 60,
    ) -> None:
        from app.adapters.ai_backup.repository import AiBackupRepository
        from app.adapters.ai_backup.session_store import AiBackupSessionStore
        from app.adapters.content.browser_auth.authenticated_context import authenticated_context

        self._cfg = cfg
        self._store = session_store or AiBackupSessionStore(db)
        self._repo = repository or AiBackupRepository(db)
        self._browser_context_factory = browser_context_factory or authenticated_context
        self._auth_probe = auth_probe or _probe_authenticated
        self._enqueue_backup = enqueue_backup or _enqueue_targeted_backup
        self._poll_interval = poll_interval_seconds
        self._timeout = flow_timeout_seconds
        self._flows: dict[str, _Flow] = {}
        self._registry_lock = asyncio.Lock()

    async def start(self, user_id: int, service: AiBackupService) -> ReauthFlowSnapshot:
        self._ensure_enabled(service)
        now = dt.datetime.now(tz=dt.UTC)
        flow = _Flow(
            id=str(uuid.uuid4()),
            user_id=user_id,
            service=service,
            state=ReauthFlowState.STARTING,
            created_at=now,
            expires_at=now + dt.timedelta(seconds=self._timeout),
        )
        async with self._registry_lock:
            for existing in self._flows.values():
                if (
                    existing.user_id == user_id
                    and existing.service == service
                    and existing.state.value in _ACTIVE_STATES
                ):
                    existing.cancelled.set()
            self._prune_terminal(now)
            self._flows[flow.id] = flow
            flow.task = asyncio.create_task(
                self._run(flow), name=f"ai-backup-reauth:{service.value}:{flow.id}"
            )
        return flow.snapshot()

    async def get(self, user_id: int, flow_id: str) -> ReauthFlowSnapshot:
        return self._owned_flow(user_id, flow_id).snapshot()

    async def capture_frame(self, user_id: int, flow_id: str) -> bytes:
        flow = self._owned_flow(user_id, flow_id)
        if flow.page is None or flow.state != ReauthFlowState.WAITING_FOR_USER:
            raise RuntimeError("interactive browser is not ready")
        async with flow.browser_lock:
            return bytes(
                await flow.page.screenshot(
                    type="jpeg", quality=72, animations="disabled", caret="hide"
                )
            )

    async def send_input(self, user_id: int, flow_id: str, event: ReauthInputEvent) -> None:
        flow = self._owned_flow(user_id, flow_id)
        if flow.page is None or flow.state != ReauthFlowState.WAITING_FOR_USER:
            raise RuntimeError("interactive browser is not ready")
        async with flow.browser_lock:
            await _apply_input(flow.page, event)

    async def cancel(self, user_id: int, flow_id: str) -> None:
        flow = self._owned_flow(user_id, flow_id)
        if flow.state.value in _TERMINAL_STATES:
            return
        flow.cancelled.set()
        if flow.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(flow.task), timeout=5)
            except TimeoutError:
                flow.task.cancel()
        if flow.state.value not in _TERMINAL_STATES:
            flow.state = ReauthFlowState.CANCELLED

    async def close(self) -> None:
        tasks: list[asyncio.Task[None]] = []
        for flow in self._flows.values():
            if flow.task is not None and not flow.task.done():
                flow.cancelled.set()
                tasks.append(flow.task)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5)
            for task in pending:
                task.cancel()

    def _owned_flow(self, user_id: int, flow_id: str) -> _Flow:
        flow = self._flows.get(flow_id)
        if flow is None or flow.user_id != user_id:
            raise KeyError(flow_id)
        return flow

    def _ensure_enabled(self, service: AiBackupService) -> None:
        enabled = self._cfg.ai_backup.enabled and (
            self._cfg.ai_backup.chatgpt_enabled
            if service == AiBackupService.CHATGPT
            else self._cfg.ai_backup.claude_enabled
        )
        if not enabled:
            raise ValueError(f"{service.value} AI backup is disabled")

    def _prune_terminal(self, now: dt.datetime) -> None:
        cutoff = now - dt.timedelta(hours=1)
        stale = [
            flow_id
            for flow_id, flow in self._flows.items()
            if flow.state.value in _TERMINAL_STATES and flow.created_at < cutoff
        ]
        for flow_id in stale:
            del self._flows[flow_id]

    async def _run(self, flow: _Flow) -> None:
        try:
            existing_state = await self._store.load(flow.user_id, flow.service)
            domain = urlparse(_LOGIN_URLS[flow.service]).hostname or ""
            refreshed_out: list[dict] = []
            authenticated = False
            async with self._browser_context_factory(
                domain,
                existing_state,
                endpoint_url=self._cfg.scraper.cloakbrowser_url,
                proxy=self._cfg.scraper.cloakbrowser_proxy,
                refreshed_out=refreshed_out,
            ) as (page, context):
                flow.page = page
                flow.context = context
                await page.goto(
                    _LOGIN_URLS[flow.service],
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                flow.state = ReauthFlowState.WAITING_FOR_USER

                while dt.datetime.now(tz=dt.UTC) < flow.expires_at:
                    if flow.cancelled.is_set():
                        flow.state = ReauthFlowState.CANCELLED
                        return
                    async with flow.browser_lock:
                        authenticated = await self._auth_probe(page, context, flow.service)
                    if authenticated:
                        flow.state = ReauthFlowState.VERIFYING
                        storage_state = dict(await context.storage_state())
                        from app.adapters.ai_backup.session_store import validate_storage_state

                        validate_storage_state(flow.service, storage_state)
                        await self._store.save(flow.user_id, flow.service, storage_state)
                        await self._repo.mark_authorization_unverified(flow.user_id, flow.service)
                        break
                    await asyncio.sleep(self._poll_interval)

            flow.page = None
            flow.context = None
            if not authenticated:
                flow.state = ReauthFlowState.EXPIRED
                return

            flow.state = ReauthFlowState.RESUMING_BACKUP
            resumed_at = dt.datetime.now(tz=dt.UTC)
            await self._enqueue_backup(flow.user_id, flow.service)
            await self._await_backup(flow, resumed_at)
        except asyncio.CancelledError:
            flow.state = ReauthFlowState.CANCELLED
            raise
        except Exception:
            logger.exception(
                "ai_backup_reauth_failed",
                extra={"flow_id": flow.id, "service": flow.service.value},
            )
            flow.state = ReauthFlowState.FAILED
            flow.error = f"Re-authorization failed. Error ID: {flow.id}"
        finally:
            flow.page = None
            flow.context = None

    async def _await_backup(self, flow: _Flow, resumed_at: dt.datetime) -> None:
        while dt.datetime.now(tz=dt.UTC) < flow.expires_at:
            if flow.cancelled.is_set():
                flow.state = ReauthFlowState.CANCELLED
                return
            row = await self._repo.get(flow.user_id, flow.service)
            attempted_after_resume = (
                row is not None
                and row.last_attempt_at is not None
                and row.last_attempt_at >= resumed_at
            )
            if attempted_after_resume:
                if (
                    row.status == AiBackupStatus.OK
                    and row.authorization_status == AiBackupAuthorizationStatus.VALID
                ):
                    flow.state = ReauthFlowState.COMPLETED
                    return
                if (
                    row.status == AiBackupStatus.FAILED
                    or row.authorization_status == AiBackupAuthorizationStatus.EXPIRED
                ):
                    flow.state = ReauthFlowState.FAILED
                    flow.error = row.last_error or (f"Backup did not resume. Error ID: {flow.id}")
                    return
            await asyncio.sleep(self._poll_interval)
        flow.state = ReauthFlowState.EXPIRED


async def _apply_input(page: Any, event: ReauthInputEvent) -> None:
    if event.type == "click" and event.x is not None and event.y is not None:
        await page.mouse.click(event.x, event.y)
        return
    if event.type == "move" and event.x is not None and event.y is not None:
        await page.mouse.move(event.x, event.y)
        return
    if event.type == "wheel":
        await page.mouse.wheel(event.delta_x or 0, event.delta_y or 0)
        return
    if event.type == "key" and event.key:
        await page.keyboard.press(event.key)
        return
    if event.type == "text" and event.text:
        await page.keyboard.insert_text(event.text)
        return
    raise ValueError("input event is missing required fields")


async def _probe_authenticated(page: Any, context: Any, service: AiBackupService) -> bool:
    cookies = await context.cookies()
    expected = (
        "__Secure-next-auth.session-token" if service == AiBackupService.CHATGPT else "sessionKey"
    )
    if not any(
        isinstance(cookie, dict)
        and (
            cookie.get("name") == expected or str(cookie.get("name", "")).startswith(f"{expected}.")
        )
        and bool(cookie.get("value"))
        for cookie in cookies
    ):
        return False

    host = (urlparse(page.url).hostname or "").lower()
    expected_host = "chatgpt.com" if service == AiBackupService.CHATGPT else "claude.ai"
    if host != expected_host and not host.endswith(f".{expected_host}"):
        return False

    script = (
        """async () => {
            const response = await fetch('/api/auth/session', {credentials: 'include', cache: 'no-store'});
            if (!response.ok) return false;
            const body = await response.json();
            return Boolean(body && body.accessToken);
        }"""
        if service == AiBackupService.CHATGPT
        else """async () => {
            const response = await fetch('/api/organizations', {credentials: 'include', cache: 'no-store'});
            if (!response.ok) return false;
            const body = await response.json();
            return Array.isArray(body) && body.length > 0;
        }"""
    )
    try:
        return bool(await page.evaluate(script))
    except Exception:
        return False


async def _enqueue_targeted_backup(user_id: int, service: AiBackupService) -> None:
    from app.tasks.ai_backup_sync import sync_one_ai_backup

    await sync_one_ai_backup.kiq(user_id, service.value)


__all__ = [
    "AiBackupReauthCoordinator",
    "ReauthFlowSnapshot",
    "ReauthFlowState",
    "ReauthInputEvent",
]
