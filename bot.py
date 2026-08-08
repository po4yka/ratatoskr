from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
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
    from app.observability.metrics_http import start_metrics_http_server_from_env
    from app.observability.otel import init_tracing

    init_tracing(cfg)

    # Size the shared offload pool before anything can use it.
    from app.core.offload import install_default_executor

    install_default_executor(cfg.runtime.offload_max_threads)

    # Docker stops a container with SIGTERM, and Python leaves it at SIG_DFL --
    # the interpreter dies on the spot. Nothing below this line ran on a deploy:
    # not the finally block's db_write_queue.stop(), not broker.shutdown(), not
    # the span flush, and not OTel's own atexit hook. Turning the signal into a
    # cancel makes the process exit through the interpreter instead, so the
    # existing teardown finally happens. SIGINT is deliberately left alone:
    # asyncio.Runner already cancels the main task on it, and overriding that
    # would lose the double-Ctrl-C escalation.
    _main_task = asyncio.current_task()
    if _main_task is not None:
        with suppress(NotImplementedError):
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, _main_task.cancel)

    start_metrics_http_server_from_env()
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
    credential_refresh: asyncio.Task[None] | None = None
    try:
        # Credentials installed through the web UI land in the API process;
        # the LLM calls happen here. Polling carries the change across the
        # container boundary and reaches live clients via the ConfigHolder
        # listeners that /setmodel already established.
        telegram_config = getattr(cfg, "telegram", None)
        owner_id = next(iter(getattr(telegram_config, "allowed_user_ids", ())), None)
        if owner_id is not None:
            from app.config.credential_reloader import start_credential_refresh_task
            from app.infrastructure.persistence.credential_store import CredentialStore

            credential_refresh = start_credential_refresh_task(
                cfg_holder, CredentialStore(db), owner_id=owner_id
            )
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
        if credential_refresh is not None:
            credential_refresh.cancel()
            with suppress(asyncio.CancelledError):
                await credential_refresh
        if bot is not None:
            try:
                await bot.message_handler.message_router.coalescer.shutdown()
            except Exception:
                logging.getLogger(__name__).warning("coalescer_shutdown_failed", exc_info=True)
        if checkpointer_runtime is not None:
            await checkpointer_runtime.stop(timeout=10.0)
        await db_write_queue.stop()
        # Hand the pooled connections back with a clean Terminate. Process exit
        # would drop them either way, but Postgres then logs each one as an
        # unexpected EOF and holds the slot until its own timeout reaps it --
        # noise that looks like a fault during an ordinary restart.
        with suppress(Exception):
            await db.dispose()
        if broker_started and broker is not None and not broker.is_worker_process:
            await broker.shutdown()
        # Last: flush the span buffer while the process is still alive. atexit
        # also covers this, but running here exports the shutdown sequence itself.
        with suppress(Exception):
            from app.observability.otel import shutdown_tracing

            shutdown_tracing()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover
        # CancelledError is our own SIGTERM handler doing its job, so it is a
        # graceful stop and must exit 0. It only surfaces here when the signal
        # lands outside the idle window -- idle() catches it and returns, so a
        # steady-state stop unwinds normally -- but a deploy that restarts a
        # container mid-startup would otherwise print a traceback and exit 1 for
        # a shutdown we asked for. The teardown finally has already run either
        # way; this only decides what the exit code says about it.
        logging.getLogger(__name__).info("shutdown")
