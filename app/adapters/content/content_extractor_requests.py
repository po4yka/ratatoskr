"""Mixin that handles request creation, deduplication, and persistence for content extraction.

Separated from the main extractor to keep the core crawl logic readable; all DB-write
paths (request rows, crawl results, message snapshots, sender metadata) live here.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.application.ports.message_persistence import MessagePersistencePort as MessagePersistence

from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger

logger = get_logger(__name__)


def schedule_crawl_persistence_task(
    *,
    cfg: Any,
    message_persistence: MessagePersistence,
    req_id: int,
    crawl: Any,
    correlation_id: str | None,
) -> asyncio.Task[None] | None:
    """Run crawl persistence off the network path."""
    try:
        task = asyncio.create_task(
            persist_crawl_result(cfg, message_persistence, req_id, crawl, correlation_id)
        )

        def _log_err(t: asyncio.Task[Any]) -> None:
            if not t.cancelled() and t.exception():
                logger.error(
                    "persist_crawl_error",
                    extra={"cid": correlation_id, "error": str(t.exception())},
                )
                try:
                    from app.observability.metrics import (
                        EXTRACTION_FAILURES,
                        PROMETHEUS_AVAILABLE,
                    )

                    if PROMETHEUS_AVAILABLE:
                        EXTRACTION_FAILURES.labels(
                            stage="persist_crawl",
                            component="background_task",
                            reason_code="exception",
                            retryable="false",
                        ).inc()
                except Exception:
                    pass

        task.add_done_callback(_log_err)
        return task
    except RuntimeError:
        return None


async def persist_crawl_result(
    cfg: Any,
    message_persistence: MessagePersistence,
    req_id: int,
    crawl: Any,
    correlation_id: str | None,
) -> None:
    """Persist crawl result; exceptions propagate so task callbacks can log and meter them."""
    try:
        retain_raw = bool(
            getattr(
                getattr(cfg, "retention", None),
                "persist_raw_extracted_content",
                True,
            )
        )
        options_json = dict(crawl.options_json or {})
        attempt_log = options_json.pop("_chain_attempt_log", None)
        winning_provider = options_json.pop("_chain_winning_provider", None)
        await message_persistence.crawl_repo.async_insert_crawl_result(
            request_id=req_id,
            success=crawl.response_success,
            markdown=crawl.content_markdown if retain_raw else None,
            html=crawl.content_html if retain_raw else None,
            error=crawl.error_text,
            metadata_json=crawl.metadata_json if retain_raw else _metadata_without_raw(crawl),
            source_url=crawl.source_url,
            http_status=crawl.http_status,
            status=crawl.status,
            endpoint=crawl.endpoint,
            latency_ms=crawl.latency_ms,
            correlation_id=crawl.correlation_id,
            options_json=options_json or None,
            attempt_log=attempt_log,
            winning_provider=winning_provider,
        )
    except Exception as e:
        raise_if_cancelled(e)
        raise


class ContentExtractorRequestsMixin:
    """Request creation/dedupe and persistence primitives."""

    # Explicit host contract: these members are provided by ContentExtractor.
    _audit: Callable[..., None]
    cfg: Any
    message_persistence: MessagePersistence

    def _schedule_crawl_persistence(
        self, req_id: int, crawl: Any, correlation_id: str | None
    ) -> asyncio.Task[None] | None:
        """Run crawl persistence off the network path."""
        try:
            task = asyncio.create_task(self._persist_crawl_result(req_id, crawl, correlation_id))

            def _log_err(t: asyncio.Task[Any]) -> None:
                if not t.cancelled() and t.exception():
                    logger.error(
                        "persist_crawl_error",
                        extra={"cid": correlation_id, "error": str(t.exception())},
                    )
                    try:
                        from app.observability.metrics import (
                            EXTRACTION_FAILURES,
                            PROMETHEUS_AVAILABLE,
                        )

                        if PROMETHEUS_AVAILABLE:
                            EXTRACTION_FAILURES.labels(
                                stage="persist_crawl",
                                component="background_task",
                                reason_code="exception",
                                retryable="false",
                            ).inc()
                    except Exception:
                        pass

            task.add_done_callback(_log_err)
            return task
        except RuntimeError:
            return None

    async def _persist_crawl_result(
        self, req_id: int, crawl: Any, correlation_id: str | None
    ) -> None:
        """Persist crawl result; exceptions propagate so the task callback can log and meter them."""
        await persist_crawl_result(
            self.cfg,
            self.message_persistence,
            req_id,
            crawl,
            correlation_id,
        )

    async def _handle_request_dedupe_or_create(
        self, message: Any, url_text: str, norm: str, dedupe: str, correlation_id: str | None
    ) -> int:
        """Handle request deduplication or creation."""
        await self._upsert_sender_metadata(message)
        try:
            req_id = await self._create_new_request(message, url_text, norm, dedupe, correlation_id)
            self._audit(
                "INFO",
                "url_request_created",
                {"request_id": req_id, "hash": dedupe, "url": url_text, "cid": correlation_id},
            )
            return req_id
        except Exception as create_error:
            existing_req = (
                await self.message_persistence.request_repo.async_get_request_by_dedupe_hash(dedupe)
            )
            if existing_req:
                req_id = int(existing_req["id"])
                if correlation_id:
                    try:
                        await self.message_persistence.request_repo.async_update_request_correlation_id(
                            req_id, correlation_id
                        )
                    except Exception as e:
                        logger.debug(
                            "correlation_id_update_failed",
                            extra={"cid": correlation_id, "error": str(e)},
                        )
                return req_id
            raise create_error

    async def _create_new_request(
        self, message: Any, url_text: str, norm: str, dedupe: str, correlation_id: str | None
    ) -> int:
        """Create a new request in the database."""
        from app.adapters.content.content_extractor import URL_ROUTE_VERSION
        from app.core.validation import (
            safe_message_id,
            safe_telegram_chat_id,
            safe_telegram_user_id,
        )
        from app.domain.models.request import RequestStatus

        chat_obj = getattr(message, "chat", None)
        chat_id_raw = getattr(chat_obj, "id", 0) if chat_obj is not None else None
        chat_id = safe_telegram_chat_id(chat_id_raw, field_name="chat_id")

        from_user_obj = getattr(message, "from_user", None)
        user_id_raw = getattr(from_user_obj, "id", 0) if from_user_obj is not None else None
        user_id = safe_telegram_user_id(user_id_raw, field_name="user_id")

        msg_id_raw = getattr(message, "id", getattr(message, "message_id", 0))
        input_message_id = safe_message_id(msg_id_raw, field_name="message_id")

        req_id = await self.message_persistence.request_repo.async_create_request(
            type_="url",
            status=RequestStatus.PENDING,
            correlation_id=correlation_id,
            chat_id=chat_id,
            user_id=user_id,
            input_url=url_text,
            normalized_url=norm,
            dedupe_hash=dedupe,
            input_message_id=input_message_id,
            content_text=url_text,
            route_version=URL_ROUTE_VERSION,
        )

        try:
            await self._persist_message_snapshot(req_id, message)
        except Exception as e:
            raise_if_cancelled(e)
            logger.error("snapshot_error", extra={"error": str(e), "cid": correlation_id})

        return req_id

    async def _upsert_sender_metadata(self, message: Any) -> None:
        """Persist sender user/chat metadata for the interaction."""
        from app.core.validation import safe_telegram_chat_id, safe_telegram_user_id

        chat_obj = getattr(message, "chat", None)
        chat_id_raw = getattr(chat_obj, "id", None) if chat_obj is not None else None
        chat_id = safe_telegram_chat_id(chat_id_raw, field_name="chat_id")
        if chat_id is not None:
            chat_type = getattr(chat_obj, "type", None)
            chat_title = getattr(chat_obj, "title", None)
            chat_username = getattr(chat_obj, "username", None)
            try:
                await self.message_persistence.user_repo.async_upsert_chat(
                    chat_id=chat_id,
                    type_=str(chat_type) if chat_type is not None else None,
                    title=str(chat_title) if isinstance(chat_title, str) else None,
                    username=str(chat_username) if isinstance(chat_username, str) else None,
                )
            except Exception as exc:
                logger.warning(
                    "chat_upsert_failed",
                    extra={"chat_id": chat_id, "error": str(exc)},
                )

        from_user_obj = getattr(message, "from_user", None)
        user_id_raw = getattr(from_user_obj, "id", None) if from_user_obj is not None else None
        user_id = safe_telegram_user_id(user_id_raw, field_name="user_id")
        if user_id is not None:
            username = getattr(from_user_obj, "username", None)
            try:
                await self.message_persistence.user_repo.async_upsert_user(
                    telegram_user_id=user_id,
                    username=str(username) if isinstance(username, str) else None,
                )
            except Exception as exc:
                logger.warning(
                    "user_upsert_failed",
                    extra={"user_id": user_id, "error": str(exc)},
                )

    async def _persist_message_snapshot(self, request_id: int, message: Any) -> None:
        """Persist message snapshot to database."""
        await self.message_persistence.persist_message_snapshot(request_id, message)


def _metadata_without_raw(crawl: Any) -> dict[str, Any]:
    source_metadata = getattr(crawl, "metadata_json", None)
    metadata: dict[str, Any] = {}
    if isinstance(source_metadata, dict):
        for key, value in source_metadata.items():
            if key not in {"raw", "raw_response", "html", "markdown", "content"}:
                metadata[key] = value
    metadata["raw_payload_persisted"] = False
    return metadata
