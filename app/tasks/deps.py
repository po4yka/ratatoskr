"""Worker-process dependency providers for TaskiqDepends.

Factories are module-level singletons (lru_cache) so each worker process
opens the DB and loads config once.  Factory helper functions that produce
fresh service objects on every task run are plain callables — not cached —
because each run needs a fresh Telethon/OpenRouter client lifecycle.

Concrete object graphs live in ``app.di.tasks``; this module re-exports them
and adds the Taskiq worker singletons (get_app_config / get_db).

``get_app_config`` hands out a ``ConfigHolder`` (cast to ``AppConfig`` for
existing call sites -- attribute reads delegate through unchanged), not a
frozen snapshot. This is the same hot-reload mechanism bot.py wires up for
the Telegram-facing LLM client: ``app.tasks.url_processing`` starts a
credential-refresh task against this holder on worker startup, so a
credential saved via the web UI reaches task runs without a process restart.
Task bodies that read ``cfg`` fresh on every invocation (digest, RSS) pick up
a rotated credential for free; a long-lived per-process client built once
(like the URL-processing runtime's LLM client) must additionally register an
``apply_runtime_config`` listener with the holder, mirroring
``app/di/shared.py``'s ``build_core_dependencies``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import cast

from taskiq import TaskiqDepends

from app.config import AppConfig, ConfigHolder
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.di.tasks import (
    DigestTaskRuntime,
    GitBackupTaskRuntime,
    RssPollTaskRuntime,
    VectorReconcileTaskRuntime,
    XBookmarksTaskRuntime,
    XWikiSyncTaskRuntime,
    build_digest_task_runtime,
    build_git_backup_task_runtime,
    build_rss_poll_task_runtime,
    build_vector_reconcile_task_runtime,
    build_x_bookmarks_task_runtime,
    build_x_wiki_sync_task_runtime,
    create_digest_bot_client,
    create_digest_llm_client,
    create_digest_service,
    create_digest_userbot,
    create_rss_bot_client,
    create_rss_delivery_service,
    create_signal_ingestion_worker,
    create_source_ingestion_runner,
)

__all__ = [
    "DigestTaskRuntime",
    "GitBackupTaskRuntime",
    "RssPollTaskRuntime",
    "VectorReconcileTaskRuntime",
    "XBookmarksTaskRuntime",
    "XWikiSyncTaskRuntime",
    "build_digest_task_runtime",
    "build_git_backup_task_runtime",
    "build_rss_poll_task_runtime",
    "build_vector_reconcile_task_runtime",
    "build_x_bookmarks_task_runtime",
    "build_x_wiki_sync_task_runtime",
    "create_digest_bot_client",
    "create_digest_llm_client",
    "create_digest_service",
    "create_digest_userbot",
    "create_rss_bot_client",
    "create_rss_delivery_service",
    "create_signal_ingestion_worker",
    "create_source_ingestion_runner",
    "get_app_config",
    "get_db",
]

# ── singleton providers ───────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _cached_config_holder() -> ConfigHolder:
    from app.config import load_config
    from app.config.worker_capacity import apply_worker_process_overrides

    return ConfigHolder(apply_worker_process_overrides(load_config()))


async def get_app_config() -> AppConfig:
    """Return the cached, hot-reloadable config for this worker process.

    See the module docstring: the returned object is actually a
    ``ConfigHolder``, cast here so existing ``TaskiqDepends(get_app_config)``
    call sites keep their ``AppConfig`` type hint.
    """
    return cast("AppConfig", _cached_config_holder())


_db_instance: Database | None = None


async def get_db(cfg: AppConfig = TaskiqDepends(get_app_config)) -> Database:
    """Return a cached Database facade for this worker process."""
    global _db_instance
    if _db_instance is None:
        from app.db.session import Database

        _db_instance = Database(config=cfg.database)
    return _db_instance
