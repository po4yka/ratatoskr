"""Taskiq task: periodic AI account backup sync (ChatGPT + Claude).

Drives ``AiBackupOrchestrationService`` for each enabled service under a Redis
lock. Notifications and healthcheck pings live here (the tasks tier) because the
Telegram bot client is built in ``app.di``, which the adapter tier may not import.

See ``docs/explanation/ai-account-backup.md`` for the full design.
"""

from __future__ import annotations

from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.db.models.ai_backup import AiBackupService
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.tasks.broker import broker
from app.tasks.deps import get_app_config, get_db

logger = get_logger(__name__)

_AI_BACKUP_SYNC_LOCK_KEY = "task_lock:ai_backup_sync"
# TTL covers the maximum expected run; 30 minutes default.
_AI_BACKUP_SYNC_LOCK_TTL = 1800


def _enabled_services(cfg: AppConfig) -> list[AiBackupService]:
    """Return the services the operator has switched on for backup."""
    services: list[AiBackupService] = []
    if cfg.ai_backup.chatgpt_enabled:
        services.append(AiBackupService.CHATGPT)
    if cfg.ai_backup.claude_enabled:
        services.append(AiBackupService.CLAUDE)
    return services


class TaskAiBackupNotifier:
    """Concrete ``BackupNotifier``: Healthchecks pings + Telegram DMs.

    Best-effort throughout — a notification or ping failure never affects the
    backup outcome. ``notify_on`` policy: ``never`` (silent), ``always`` (success
    and failure), ``failure`` (failure + auth-expiry only). Auth-expiry is always
    surfaced when a chat id is configured and ``notify_on`` is not ``never``,
    because it needs operator action.
    """

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg

    async def on_start(self, service: AiBackupService) -> None:
        await self._ping("start")

    async def on_success(
        self, service: AiBackupService, counts: dict[str, int], correlation_id: str
    ) -> None:
        await self._ping("")
        if self._cfg.ai_backup.notify_on == "always":
            summary = ", ".join(f"{k}={v}" for k, v in counts.items())
            await self._send(f"AI backup ok [{service.value}]: {summary}")

    async def on_failure(self, service: AiBackupService, correlation_id: str) -> None:
        await self._ping("fail")
        if self._cfg.ai_backup.notify_on in ("always", "failure"):
            await self._send(
                f"AI backup FAILED [{service.value}] (correlation_id={correlation_id})"
            )

    async def on_auth_expired(self, service: AiBackupService, correlation_id: str) -> None:
        await self._ping("fail")
        if self._cfg.ai_backup.notify_on != "never":
            await self._send(
                f"AI backup session EXPIRED for [{service.value}]. "
                f"Re-supply a session via /ai_backup_login {service.value}."
            )

    async def _ping(self, phase: str) -> None:
        base = self._cfg.ai_backup.hc_ping_url
        if not base:
            return
        url = base if not phase else f"{base.rstrip('/')}/{phase}"
        try:
            import httpx

            timeout = self._cfg.ai_backup.hc_ping_timeout_seconds
            async with httpx.AsyncClient(timeout=timeout) as client:
                await client.post(url)
        except Exception as exc:
            logger.debug("ai_backup_hc_ping_failed", extra={"phase": phase, "error": str(exc)})

    async def _send(self, text: str) -> None:
        chat_id = self._cfg.ai_backup.notify_chat_id
        if chat_id is None:
            return
        try:
            from app.tasks.deps import create_digest_bot_client

            bot = create_digest_bot_client(self._cfg)
            async with bot:
                await bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            logger.warning("ai_backup_notify_failed", extra={"chat_id": chat_id, "error": str(exc)})


@broker.task(task_name="ratatoskr.ai_backup.sync")
async def sync_ai_backup(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> None:
    """Back up the deployment owner's enabled AI web accounts."""
    if not cfg.ai_backup.enabled:
        logger.info("ai_backup_sync_disabled")
        return

    services = _enabled_services(cfg)
    if not services:
        logger.info("ai_backup_sync_no_services_enabled")
        return

    owner_id = next(iter(cfg.telegram.allowed_user_ids), None)
    if owner_id is None:
        logger.warning(
            "ai_backup_sync_no_owner",
            extra={"reason": "ALLOWED_USER_IDS is empty; cannot key backup rows"},
        )
        return

    redis_client = await get_redis(cfg)
    async with RedisDistributedLock(
        redis_client, _AI_BACKUP_SYNC_LOCK_KEY, _AI_BACKUP_SYNC_LOCK_TTL
    ) as acquired:
        if not acquired:
            logger.info(
                "ai_backup_sync_skipped_lock_held",
                extra={"key": _AI_BACKUP_SYNC_LOCK_KEY},
            )
            return

        await _run_sync(cfg, db, owner_id=owner_id, services=services)


async def _run_sync(
    cfg: AppConfig,
    db: Database,
    *,
    owner_id: int,
    services: list[AiBackupService],
) -> None:
    """Run the orchestration service for each enabled service."""
    from app.adapters.ai_backup.repository import AiBackupRepository
    from app.adapters.ai_backup.service import AiBackupOrchestrationService
    from app.adapters.ai_backup.session_store import AiBackupSessionStore

    repo = AiBackupRepository(db)
    session_store = AiBackupSessionStore(db)
    notifier = TaskAiBackupNotifier(cfg)
    svc = AiBackupOrchestrationService(
        cfg=cfg, repo=repo, session_store=session_store, notifier=notifier
    )

    for service in services:
        try:
            await svc.run(owner_id, service)
        except Exception as exc:
            # AiBackupAuthExpiredError does not reach here (run() returns early).
            logger.error(
                "ai_backup_service_run_failed",
                extra={"service": service.value, "error": str(exc)},
            )
