"""Edge helpers for durable request processing enqueue."""

from __future__ import annotations

from app.core.logging_utils import get_logger
from app.di.api import get_current_api_runtime

logger = get_logger(__name__)


async def process_url_request(
    request_id: int, db_path: str | None = None, correlation_id: str | None = None
) -> None:
    if db_path is not None:
        logger.warning(
            "durable_request_enqueue_ignores_db_path",
            extra={"request_id": request_id, "db_path": db_path},
        )
    runtime = get_current_api_runtime()
    await runtime.durable_request_queue.enqueue(
        request_id=request_id,
        correlation_id=correlation_id,
    )
