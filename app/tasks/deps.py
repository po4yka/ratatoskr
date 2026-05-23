"""Worker-process dependency providers for TaskiqDepends.

Factories are module-level singletons (lru_cache) so each worker process
opens the DB and loads config once.  Factory helper functions that produce
fresh service objects on every task run are plain callables — not cached —
because each run needs a fresh Telethon/OpenRouter client lifecycle.

Concrete object graphs live in ``app.di.tasks`` so future jobs can reuse
runtime bundles instead of copying wiring here.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.di.tasks import (
    DigestTaskRuntime,
    XBookmarksTaskRuntime,
    XWikiSyncTaskRuntime,
    RssPollTaskRuntime,
    VectorReconcileTaskRuntime,
)

# ── singleton providers ───────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _cached_config() -> AppConfig:
    from app.config import load_config

    return load_config()


async def get_app_config() -> AppConfig:
    """Return the cached AppConfig singleton for this worker process."""
    return _cached_config()


_db_instance: Database | None = None


async def get_db(cfg: AppConfig = TaskiqDepends(get_app_config)) -> Database:
    """Return a cached Database facade for this worker process."""
    global _db_instance
    if _db_instance is None:
        from app.db.session import Database

        _db_instance = Database(config=cfg.database)
    return _db_instance


# ── digest factory helpers ────────────────────────────────────────────────────


def create_digest_userbot(cfg: AppConfig) -> Any:
    from app.di.tasks import create_digest_userbot as _create_digest_userbot

    return _create_digest_userbot(cfg)


def create_digest_llm_client(cfg: AppConfig) -> Any:
    from app.di.tasks import create_digest_llm_client as _create_digest_llm_client

    return _create_digest_llm_client(cfg)


def create_digest_bot_client(cfg: AppConfig) -> Any:
    from app.di.tasks import create_digest_bot_client as _create_digest_bot_client

    return _create_digest_bot_client(cfg)


def create_digest_service(
    cfg: AppConfig,
    *,
    userbot: Any,
    llm_client: Any,
    send_message: Any,
) -> Any:
    from app.di.tasks import create_digest_service as _create_digest_service

    return _create_digest_service(
        cfg,
        userbot=userbot,
        llm_client=llm_client,
        send_message=send_message,
    )


def build_digest_task_runtime(cfg: AppConfig) -> DigestTaskRuntime:
    """Return digest task runtime using this module's delegated factories."""
    return DigestTaskRuntime(
        cfg=cfg,
        userbot_factory=create_digest_userbot,
        llm_client_factory=create_digest_llm_client,
        bot_client_factory=create_digest_bot_client,
        service_factory=create_digest_service,
    )


# ── RSS / signal factory helpers ──────────────────────────────────────────────


def create_rss_bot_client(cfg: AppConfig) -> Any:
    from app.di.tasks import create_rss_bot_client as _create_rss_bot_client

    return _create_rss_bot_client(cfg)


def create_rss_delivery_service(cfg: AppConfig, db: Database) -> Any:
    from app.di.tasks import create_rss_delivery_service as _create_rss_delivery_service

    return _create_rss_delivery_service(cfg, db)


def create_signal_ingestion_worker(cfg: AppConfig, db: Database) -> Any:
    from app.di.tasks import create_signal_ingestion_worker as _create_signal_ingestion_worker

    return _create_signal_ingestion_worker(cfg, db)


def create_source_ingestion_runner(cfg: AppConfig, db: Database) -> Any:
    from app.di.tasks import create_source_ingestion_runner as _create_source_ingestion_runner

    return _create_source_ingestion_runner(cfg, db)


def build_rss_poll_task_runtime(cfg: AppConfig, db: Database) -> RssPollTaskRuntime:
    """Return RSS poll task runtime using this module's delegated factories."""
    return RssPollTaskRuntime(
        cfg=cfg,
        db=db,
        bot_client_factory=create_rss_bot_client,
        delivery_service_factory=create_rss_delivery_service,
        signal_worker_factory=create_signal_ingestion_worker,
        source_runner_factory=create_source_ingestion_runner,
    )


def build_vector_reconcile_task_runtime(
    cfg: AppConfig,
    db: Database,
) -> VectorReconcileTaskRuntime:
    from app.di.tasks import build_vector_reconcile_task_runtime as _build_runtime

    return _build_runtime(cfg, db)


def build_x_bookmarks_task_runtime(
    cfg: AppConfig,
    db: Database,
) -> XBookmarksTaskRuntime:
    from app.di.tasks import build_x_bookmarks_task_runtime as _build_runtime

    return _build_runtime(cfg, db)


def build_x_wiki_sync_task_runtime(
    cfg: AppConfig,
    db: Database,
) -> XWikiSyncTaskRuntime:
    from app.di.tasks import build_x_wiki_sync_task_runtime as _build_runtime

    return _build_runtime(cfg, db)
