"""Persistence and cache helpers for Telegram callback actions."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger
from app.infrastructure.persistence.digest_store import DigestStore

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


class CallbackActionStore:
    """Owns DB-backed lookups and short-lived callback payload caching."""

    def __init__(
        self,
        *,
        request_repo: Any,
        summary_repo: Any,
        asyncio_module: Any = asyncio,
        time_module: Any = time,
        summary_cache_ttl: float = 30.0,
        summary_cache_max: int = 50,
    ) -> None:
        self._request_repo = request_repo
        self._summary_repo = summary_repo
        self._digest_store = DigestStore()
        self._asyncio = asyncio_module
        self._time = time_module
        self._summary_cache_ttl = summary_cache_ttl
        self._summary_cache_max = summary_cache_max
        self._summary_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def get_digest_post(self, channel_id: int, message_id: int) -> Any:
        return await self._asyncio.to_thread(
            self._digest_store.get_channel_post,
            channel_id=channel_id,
            message_id=message_id,
        )

    async def toggle_save(self, summary_id: str) -> bool | None:
        if summary_id.startswith("req:"):
            summary = await self._summary_repo.async_get_summary_by_request(int(summary_id[4:]))
            if summary is None:
                return None
            summary_id_int = int(summary["id"])
        else:
            summary_id_int = int(summary_id)
        new_state = await self._summary_repo.async_toggle_favorite(summary_id_int)
        self._summary_cache.pop(summary_id, None)
        return new_state

    async def lookup_retry_url(self, correlation_id: str) -> str | None:
        request = await self._request_repo.async_get_latest_request_by_correlation_id(
            correlation_id
        )
        if request is None:
            return None
        return str(request.get("input_url") or "") or None

    async def load_summary_payload(
        self,
        summary_id: str,
        *,
        correlation_id: str | None = None,
        cache: dict[str, tuple[float, dict[str, Any]]] | None = None,
        loader: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any] | None:
        """Load summary JSON payload from database with short-lived caching."""
        active_cache = self._summary_cache if cache is None else cache
        active_loader = self._load_summary_payload_sync if loader is None else loader

        now = self._time.time()
        cached = active_cache.get(summary_id)
        if cached is not None:
            cached_at, cached_payload = cached
            if now - cached_at < self._summary_cache_ttl:
                return cached_payload

        try:
            if loader is None:
                result = await self._load_summary_payload(summary_id)
            else:
                result = await self._asyncio.to_thread(active_loader, summary_id)
            if result is not None:
                if len(active_cache) >= self._summary_cache_max:
                    oldest_key = min(active_cache, key=lambda key: active_cache[key][0])
                    active_cache.pop(oldest_key, None)
                active_cache[summary_id] = (now, result)
            return result
        except Exception as exc:
            logger.exception(
                "load_summary_payload_failed",
                extra={"summary_id": summary_id, "error": str(exc), "cid": correlation_id},
            )
            return None

    async def _load_summary_payload(self, summary_id: str) -> dict[str, Any] | None:
        if summary_id.startswith("req:"):
            request_id = int(summary_id[4:])
            summary = await self._summary_repo.async_get_summary_by_request(request_id)
            if summary is None:
                return None
            request = await self._request_repo.async_get_request_by_id(request_id)
        else:
            context = await self._summary_repo.async_get_summary_context_by_id(int(summary_id))
            if context is None:
                return None
            summary = context.get("summary") if isinstance(context, dict) else None
            request = context.get("request") if isinstance(context, dict) else None

        if not isinstance(summary, dict):
            return None

        url = request.get("normalized_url") if isinstance(request, dict) else None

        payload = summary.get("json_payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        return {
            "id": str(summary.get("id")),
            "request_id": summary.get("request"),
            "url": url,
            "lang": summary.get("lang"),
            "insights": summary.get("insights_json")
            if isinstance(summary.get("insights_json"), dict)
            else None,
            **payload,
        }

    def _load_summary_payload_sync(self, summary_id: str) -> dict[str, Any] | None:
        raise RuntimeError("Synchronous summary loading requires an explicit test loader")
