"""Cached summary lookup and reply delivery for URL flows."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger, redact_url_for_logging

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )

from app.adapters.content.url_flow_models import (
    URLProcessingFlowResult,
    create_chunk_llm_stub,
)
from app.core.url_utils import compute_dedupe_hash
from app.db.user_interactions import async_safe_update_user_interaction

logger = get_logger(__name__)


class CachedSummaryResponder:
    """Own cache-hit lookup, payload decoding, and cached reply delivery."""

    def __init__(
        self,
        *,
        cfg: Any,
        db: Any,
        response_formatter: ResponseFormatter,
        request_repo: Any,
        summary_repo: Any,
    ) -> None:
        self._cfg = cfg
        self._db = db
        self._response_formatter = response_formatter
        self._request_repo = request_repo
        self._summary_repo = summary_repo

    async def maybe_reply(
        self,
        message: Any,
        url_text: str,
        *,
        correlation_id: str | None = None,
        interaction_id: int | None = None,
        silent: bool = False,
    ) -> URLProcessingFlowResult | None:
        """Reply with a cached summary when a matching request already exists."""
        try:
            dedupe_hash = compute_dedupe_hash(url_text)
            request_row = await self._request_repo.async_get_request_by_dedupe_hash(dedupe_hash)
            request_id = request_row.get("id") if isinstance(request_row, dict) else None
            if not isinstance(request_id, int):
                return None

            cached = await self._summary_repo.async_get_summary_by_request(request_id)
            payload = self._decode_payload(
                cached.get("json_payload") if isinstance(cached, dict) else None
            )
            if not isinstance(payload, dict):
                return None

            logger.info(
                "cache_hit",
                extra={
                    "cid": correlation_id,
                    "url": redact_url_for_logging(url_text),
                    "hash": dedupe_hash[:12],
                },
            )
            if correlation_id:
                await self._update_request_correlation_id(request_id, correlation_id)

            if not silent:
                await self._response_formatter.send_cached_summary_notification(
                    message, silent=silent
                )
                await self._response_formatter.send_structured_summary_response(
                    message,
                    payload,
                    create_chunk_llm_stub(self._cfg),
                    summary_id=f"req:{request_id}" if request_id else None,
                )

            if interaction_id:
                await async_safe_update_user_interaction(
                    self._db,
                    interaction_id=interaction_id,
                    response_sent=True,
                    response_type="summary",
                    request_id=request_id,
                )

            return URLProcessingFlowResult.from_summary(
                payload,
                cached=True,
                request_id=request_id,
            )
        except Exception as exc:
            logger.warning(
                "cache_check_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )
            return None

    async def _update_request_correlation_id(
        self,
        request_id: int,
        correlation_id: str,
    ) -> None:
        try:
            await self._request_repo.async_update_request_correlation_id(request_id, correlation_id)
        except Exception as exc:
            logger.warning(
                "cache_hit_cid_update_failed",
                extra={"error": str(exc), "cid": correlation_id},
            )

    def _decode_payload(self, payload: Any) -> dict[str, Any] | None:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError:
                return None
            return decoded if isinstance(decoded, dict) else None
        return None
