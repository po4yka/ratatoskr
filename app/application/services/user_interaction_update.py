"""User-interaction persistence helper (application layer).

Moved here from ``app.db.user_interactions`` (audit finding A5) so the
summarization orchestration under ``app/application/`` can update interactions
without importing the ``app.db`` layer (which the layering contract forbids).
``app.db.user_interactions`` re-exports these for its remaining adapter/db-layer
callers, and the duck-typed ``user_repo`` arg works with either a
``UserRepositoryPort`` or a ``Database`` exposing ``async_update_user_interaction``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    import logging

    from app.application.ports.users import UserRepositoryPort

logger = get_logger(__name__)


async def async_safe_update_user_interaction(
    user_repo: UserRepositoryPort | Any,
    *,
    interaction_id: int | None,
    logger_: logging.Logger | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
    updates: dict[str, Any] | None = None,
    **fields: Any,
) -> None:
    """Async counterpart to :func:`safe_update_user_interaction`."""
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
        await user_repo.async_update_user_interaction(
            interaction_id=interaction_id,
            updates=update_mapping,
            **payload,
        )
    except Exception as exc:
        log = logger_ if logger_ is not None else logger
        log.warning(
            "user_interaction_update_failed",
            extra={"interaction_id": interaction_id, "error": str(exc)},
        )


def _prepare_interaction_update(
    interaction_id: int | None,
    *,
    updates: dict[str, Any] | None,
    start_time: float | None,
    end_time: float | None,
    fields: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """Normalize arguments shared between sync and async helpers."""
    if interaction_id is None or interaction_id <= 0:
        return None

    if updates is not None and fields:
        msg = "Cannot mix 'updates' with individual field arguments"
        raise ValueError(msg)

    payload = dict(fields)

    if start_time is not None and "processing_time_ms" not in payload and updates is None:
        stop_time = end_time if end_time is not None else time.time()
        duration_ms = max(0, int((stop_time - start_time) * 1000))
        payload["processing_time_ms"] = duration_ms

    return payload, updates
