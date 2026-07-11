from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from app.adapters.telegram.telegram_bot import TelegramBot
from app.config import ConfigHolder, load_config
from app.db.write_queue import DbWriteQueue
from app.di.database import build_runtime_database
from app.di.repositories import build_audit_log_repository
from app.di.telegram import build_telegram_runtime

# Use uvloop for better async performance if available
try:
    import uvloop

    uvloop.install()
except ImportError:  # pragma: no cover
    uvloop = None


async def main() -> None:
    cfg = load_config()
    from app.observability.otel import init_tracing

    init_tracing(cfg)
    cfg_holder = ConfigHolder(cfg)

    # Log active model configuration at startup
    _log = logging.getLogger(__name__)
    _log.info(
        "models_config_active",
        extra={
            "openrouter_primary": cfg.openrouter.model,
            "openrouter_fallbacks": list(cfg.openrouter.fallback_models),
            "openrouter_flash": cfg.openrouter.flash_model,
            "openrouter_flash_fallbacks": list(cfg.openrouter.flash_fallback_models),
            "routing_enabled": cfg.model_routing.enabled,
            "routing_default": cfg.model_routing.default_model,
            "routing_technical": cfg.model_routing.technical_model,
            "routing_sociopolitical": cfg.model_routing.sociopolitical_model,
            "routing_long_context": cfg.model_routing.long_context_model,
            "vision_model": cfg.attachment.vision_model,
        },
    )

    # Warn if DB path is not under /data when likely running in Docker (non-persistent)
    if not cfg.runtime.db_path.startswith("/data/"):
        logging.getLogger(__name__).warning(
            "db_path_not_in_data_volume", extra={"db_path": cfg.runtime.db_path}
        )
    # `build_runtime_database(..., migrate=True)` calls asyncio.run() internally
    # and crashes when invoked from a running event loop (such as bot.py's
    # asyncio.run(main()) entry point). Construct without migrate, then run
    # migrations in the surrounding async context.
    db = build_runtime_database(cfg, self_heal=True)
    await db.migrate()

    db_write_queue = DbWriteQueue(maxsize=256)
    db_write_queue.start()

    # Start the taskiq broker in producer mode (not worker mode — the worker
    # process runs separately via `taskiq worker app.tasks.broker:broker`).
    broker: Any = None
    broker_started = False
    try:
        from app.tasks.broker import broker as _broker

        broker = _broker
        if not broker.is_worker_process:
            await broker.startup()
            broker_started = True
    except ImportError:
        broker = None

    # Start the LangGraph Postgres checkpointer when enabled (opt-in,
    # failure-isolated; ADR-0004). Dedicated psycopg3 pool -- not Database.
    # Started inside the try so the finally below always tears the pool down.
    checkpointer_runtime: Any = None
    bot: TelegramBot | None = None
    try:
        if cfg.langgraph_checkpoint.enabled:
            try:
                from app.infrastructure.checkpointing import CheckpointerRuntime

                checkpointer_runtime = CheckpointerRuntime(cfg=cfg)
                await checkpointer_runtime.start()
            except Exception:
                logging.getLogger(__name__).warning(
                    "langgraph_checkpointer_startup_failed", exc_info=True
                )
                checkpointer_runtime = None
        bot = TelegramBot(
            cfg=cfg_holder,  # type: ignore[arg-type]
            db=db,
            runtime_builder=partial(
                build_telegram_runtime,
                checkpointer=checkpointer_runtime.saver
                if checkpointer_runtime is not None
                else None,
            ),
            audit_repository_builder=build_audit_log_repository,
            db_write_queue=db_write_queue,
        )
        await bot.start()
    finally:
        if bot is not None:
            try:
                await bot.message_handler.message_router.coalescer.shutdown()
            except Exception:
                logging.getLogger(__name__).warning("coalescer_shutdown_failed", exc_info=True)
        if checkpointer_runtime is not None:
            await checkpointer_runtime.stop(timeout=10.0)
        await db_write_queue.stop()
        if broker_started and broker is not None and not broker.is_worker_process:
            await broker.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover
        logging.getLogger(__name__).info("shutdown")
