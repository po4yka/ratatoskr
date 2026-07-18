"""SQLAlchemy implementation of the crawl-result repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

if TYPE_CHECKING:
    from app.db.session import Database

from app.db.json_utils import prepare_json_payload
from app.db.models import CrawlResult, Request, model_to_dict


class CrawlResultRepositoryAdapter:
    """Adapter that implements crawl-result persistence via SQLAlchemy."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_insert_crawl_result(
        self,
        request_id: int,
        success: bool,
        markdown: str | None = None,
        html: str | None = None,
        error: str | None = None,
        metadata_json: dict[str, Any] | None = None,
        *,
        source_url: str | None = None,
        http_status: int | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        latency_ms: int | None = None,
        correlation_id: str | None = None,
        options_json: dict[str, Any] | None = None,
        attempt_log: list[dict[str, Any]] | None = None,
        winning_provider: str | None = None,
    ) -> int:
        """Insert a crawl result and return an existing row on request-id conflict."""
        payload = {
            "request_id": request_id,
            "firecrawl_success": success,
            "content_markdown": markdown,
            "content_html": html,
            "error_text": error,
            "metadata_json": prepare_json_payload(metadata_json, default={}),
            "source_url": source_url,
            "http_status": http_status,
            "status": status,
            "endpoint": endpoint,
            "latency_ms": latency_ms,
            "correlation_id": correlation_id,
            "options_json": prepare_json_payload(options_json, default=None),
            "attempt_log": prepare_json_payload(attempt_log, default=None),
            "winning_provider": winning_provider,
        }
        async with self._database.transaction() as session:
            stmt = (
                insert(CrawlResult)
                .values(**payload)
                .on_conflict_do_nothing(index_elements=[CrawlResult.request_id])
                .returning(CrawlResult.id)
            )
            inserted_id = await session.scalar(stmt)
            if inserted_id is not None:
                return int(inserted_id)
            existing_id = await session.scalar(
                select(CrawlResult.id).where(CrawlResult.request_id == request_id)
            )
            if existing_id is None:
                msg = f"crawl result conflict for request_id={request_id} but no row exists"
                raise RuntimeError(msg)
            return int(existing_id)

    async def async_get_crawl_result_by_request(self, request_id: int) -> dict[str, Any] | None:
        """Get a crawl result by request ID."""
        async with self._database.session() as session:
            result = await session.scalar(
                select(CrawlResult).where(CrawlResult.request_id == request_id)
            )
            return model_to_dict(result)

    async def async_get_max_server_version(self, user_id: int) -> int | None:
        """Return the maximum server_version across crawl results owned by *user_id*."""
        async with self._database.session() as session:
            value = await session.scalar(
                select(func.max(CrawlResult.server_version))
                .join(Request, CrawlResult.request_id == Request.id)
                .where(Request.user_id == user_id)
            )
            return int(value) if value is not None else None

    async def async_get_all_for_user(self, user_id: int, *, since: int = 0) -> list[dict[str, Any]]:
        """Get all crawl results for a user, with request_id flattened.

        ``since`` pushes the sync cursor into the query so a poll only reads rows
        changed past it, instead of the user's entire lifetime history (audit #2).
        """
        stmt = (
            select(CrawlResult)
            .join(Request, CrawlResult.request_id == Request.id)
            .where(Request.user_id == user_id)
        )
        if since > 0:
            stmt = stmt.where(CrawlResult.server_version > since)
        stmt = stmt.order_by(CrawlResult.id)
        async with self._database.session() as session:
            rows = (await session.execute(stmt)).scalars()
            return [model_to_dict(row) or {} for row in rows]
