"""Taskiq task: periodic AI account backup sync (ChatGPT + Claude).

P0 skeleton. This wires the schedule, the Redis lock, the per-service lifecycle
rows, and the notification hook so the subsystem is observable end-to-end while
it ships disabled by default. The actual authenticated-session scraping (open a
persistent CloakBrowser context, walk the internal APIs, write artifacts to
disk) is implemented in later phases; this run materializes a lifecycle row per
enabled service and logs a stub.

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
    """Materialize lifecycle rows and log the P0 stub for each enabled service."""
    from app.adapters.ai_backup.repository import AiBackupRepository

    repo = AiBackupRepository(db)
    for service in services:
        try:
            row = await repo.ensure(owner_id, service)
        except Exception as exc:
            logger.warning(
                "ai_backup_ensure_failed",
                extra={"service": service.value, "error": str(exc)},
            )
            continue

        # P0: execution not yet implemented. Later phases attach the
        # authenticated CloakBrowser session + provider clients here.
        logger.info(
            "ai_backup_sync_stub",
            extra={
                "service": service.value,
                "row_id": row.id,
                "status": row.status.value,
                "owner_id": owner_id,
                "note": "backup execution not yet implemented (P1)",
            },
        )

    await _send_telegram_notify(cfg, services)


async def _send_telegram_notify(cfg: AppConfig, services: list[AiBackupService]) -> None:
    """Send a Telegram completion DM when notify_on == 'always'.

    Best-effort: any error is logged at WARNING and swallowed. Uses the
    established ``create_digest_bot_client`` pattern.
    """
    ai_cfg = cfg.ai_backup
    chat_id = ai_cfg.notify_chat_id
    if chat_id is None or ai_cfg.notify_on != "always":
        return

    text = "AI account backup run complete (P0 stub): " + ", ".join(s.value for s in services)
    try:
        from app.tasks.deps import create_digest_bot_client

        bot = create_digest_bot_client(cfg)
        async with bot:
            await bot.send_message(chat_id=chat_id, text=text)
    except Exception as exc:
        logger.warning(
            "ai_backup_notify_failed",
            extra={"chat_id": chat_id, "error": str(exc)},
        )
