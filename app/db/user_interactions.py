"""Helper utilities for working with user interaction persistence."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.application.services.user_interaction_update import (
    _prepare_interaction_update,
    async_safe_update_user_interaction,  # noqa: F401  re-exported for adapter/db-layer callers
)
from app.core.logging_utils import get_logger, log_exception
from app.infrastructure.persistence.repositories.user_repository import (
    UserRepositoryAdapter,
)

if TYPE_CHECKING:
    import logging

    from app.db.session import Database

logger = get_logger(__name__)


_update_tasks: set[asyncio.Task[None]] = set()


def safe_update_user_interaction(
    db: Database | Any,
    *,
    interaction_id: int | None,
    logger_: logging.Logger | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
    updates: dict[str, Any] | None = None,
    **fields: Any,
) -> None:
    """Sync helper for updating user interaction (legacy, prefer async version)."""
    # This helper is legacy and should be avoided in async code
    prepared = _prepare_interaction_update(
        interaction_id,
        updates=updates,
        start_time=start_time,
        end_time=end_time,
        fields=fields,
    )

    if prepared is None:
        return

    payload, update_mapping = prepared

    try:
        if hasattr(db, "update_user_interaction"):
            db.update_user_interaction(
                interaction_id,
                updates=update_mapping,
                **payload,
            )
            return

        if hasattr(db, "async_update_user_interaction"):
            coro = db.async_update_user_interaction(
                interaction_id=interaction_id,
                updates=update_mapping,
                **payload,
            )
        else:
            user_repo = UserRepositoryAdapter(db)
            coro = user_repo.async_update_user_interaction(
                interaction_id=interaction_id,
                updates=update_mapping,
                **payload,
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
        else:
            task = loop.create_task(coro)
            _update_tasks.add(task)

            def _on_task_done(t: asyncio.Task[None]) -> None:
                _update_tasks.discard(t)
                if t.cancelled():
                    return
                exc = t.exception()
                if exc:
                    log = logger_ if logger_ is not None else logger
                    log_exception(
                        log,
                        "user_interaction_update_task_failed",
                        exc,
                        level="warning",
                        interaction_id=interaction_id,
                    )

            task.add_done_callback(_on_task_done)
    except Exception as exc:
        log = logger_ if logger_ is not None else logger
        log.warning(
            "user_interaction_update_failed",
            extra={"interaction_id": interaction_id, "error": str(exc)},
        )
