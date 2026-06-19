"""Compatibility facade for Telegram callback action handlers."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from app.adapters.telegram.callback_action_io_handlers import CallbackActionIOHandlers
from app.adapters.telegram.callback_action_presenters import CallbackActionPresenters
from app.adapters.telegram.callback_action_store import CallbackActionStore
from app.adapters.telegram.callback_action_summary_handlers import (
    CallbackActionSummaryHandlers,
)

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.url_handler import URLHandler
    from app.db.session import Database
    from app.infrastructure.search.hybrid_search_service import HybridSearchService

# Timeout constants for expensive callback operations (seconds).
_CB_TIMEOUT_LLM = 120.0
_CB_TIMEOUT_SEARCH = 30.0
_CB_TIMEOUT_DIGEST = 180.0
_CB_TIMEOUT_EXPORT = 60.0

# Simple TTL cache for load_summary_payload() to avoid redundant DB queries
# when the same summary is accessed by multiple button clicks.
_SUMMARY_CACHE_TTL = 30.0
_SUMMARY_CACHE_MAX = 50


class CallbackActionService:
    """Expose the historic callback action API through focused collaborators."""

    def __init__(
        self,
        db: Database,
        response_formatter: ResponseFormatter,
        url_handler: URLHandler | None = None,
        hybrid_search: HybridSearchService | None = None,
        lang: str = "en",
        request_repo: Any | None = None,
        summary_repo: Any | None = None,
    ) -> None:
        self.db = db
        self.response_formatter = response_formatter
        self.url_handler = url_handler
        self.hybrid_search = hybrid_search
        self._lang = lang

        self._presenters = CallbackActionPresenters(lang=lang)
        self._store = CallbackActionStore(
            request_repo=request_repo,
            summary_repo=summary_repo,
            asyncio_module=asyncio,
            time_module=time,
            summary_cache_ttl=_SUMMARY_CACHE_TTL,
            summary_cache_max=_SUMMARY_CACHE_MAX,
        )
        self._summary_handlers = CallbackActionSummaryHandlers(
            response_formatter=response_formatter,
            store=self._store,
            presenters=self._presenters,
            url_handler=url_handler,
            hybrid_search=hybrid_search,
            lang=lang,
            llm_timeout=_CB_TIMEOUT_LLM,
            search_timeout=_CB_TIMEOUT_SEARCH,
            asyncio_module=asyncio,
        )
        self._io_handlers = CallbackActionIOHandlers(
            db=db,
            response_formatter=response_formatter,
            store=self._store,
            presenters=self._presenters,
            url_handler=url_handler,
            lang=lang,
            digest_timeout=_CB_TIMEOUT_DIGEST,
            export_timeout=_CB_TIMEOUT_EXPORT,
            llm_timeout=_CB_TIMEOUT_LLM,
            asyncio_module=asyncio,
        )

        # Preserve easy access for existing tests and internal callers.
        self._summary_cache = self._store._summary_cache
        self._load_summary_payload_sync = self._store._load_summary_payload_sync

    async def handle_digest_full_summary(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        return await self._io_handlers.handle_digest_full_summary(
            message,
            uid,
            parts,
            correlation_id,
        )

    async def handle_export(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        return await self._io_handlers.handle_export(message, uid, parts, correlation_id)

    async def handle_translate(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        self._summary_handlers.bind_summary_loader(self.load_summary_payload)
        return await self._summary_handlers.handle_translate(message, uid, parts, correlation_id)

    async def handle_find_similar(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        self._summary_handlers.bind_summary_loader(self.load_summary_payload)
        return await self._summary_handlers.handle_find_similar(
            message,
            uid,
            parts,
            correlation_id,
        )

    async def handle_toggle_save(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        return await self._summary_handlers.handle_toggle_save(
            message,
            uid,
            parts,
            correlation_id,
        )

    async def handle_rate(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        return await self._summary_handlers.handle_rate(message, uid, parts, correlation_id)

    async def handle_more(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        self._summary_handlers.bind_summary_loader(self.load_summary_payload)
        return await self._summary_handlers.handle_more(message, uid, parts, correlation_id)

    async def handle_show_related_summary(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        self._summary_handlers.bind_summary_loader(self.load_summary_payload)
        return await self._summary_handlers.handle_show_related_summary(
            message,
            uid,
            parts,
            correlation_id,
        )

    async def load_summary_payload(
        self,
        summary_id: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._store.load_summary_payload(
            summary_id,
            correlation_id=correlation_id,
            cache=self._summary_cache,
        )

    async def handle_retry(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        return await self._io_handlers.handle_retry(message, uid, parts, correlation_id)
