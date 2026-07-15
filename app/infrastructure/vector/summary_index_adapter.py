"""Qdrant-backed read-your-writes summary indexer (ADR-0012).

Implements :class:`app.application.ports.summary_index.SummaryIndexPort`. Embeds
and upserts a summary's point SYNCHRONOUSLY on the persist path so a new summary
is retrievable before the next reconciler pass. The point is byte-identical to the
one the reconciler writes for the same summary -- same point id, same indexable
text (hence same vector), same payload -- because both build it from
:mod:`app.infrastructure.vector.summary_point`. Fast path = freshness;
the reconciler = convergence/backfill.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger
from app.infrastructure.vector.point_ids import summary_point_id
from app.infrastructure.vector.summary_point import (
    build_summary_qdrant_payload,
    coerce_summary_payload,
    extract_indexable_text,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.application.dto.vector_search import RetrievalScope

logger = get_logger(__name__)


class QdrantSummaryIndexAdapter:
    """Embed + upsert a summary point synchronously (read-your-writes fast-path)."""

    def __init__(
        self,
        *,
        vector_store: Any,
        embedding_service: Any,
        embedding_repository: Any | None = None,
    ) -> None:
        self._vector_store = vector_store
        self._embedding_service = embedding_service
        self._embedding_repository = embedding_repository

    async def index_summary(
        self,
        *,
        request_id: int,
        summary_id: int,
        summary: Mapping[str, Any],
        lang: str | None,
        scope: RetrievalScope,
        correlation_id: str | None = None,
    ) -> None:
        payload_dict, raw_fallback = coerce_summary_payload(dict(summary))
        text = extract_indexable_text(payload_dict, raw_fallback=raw_fallback)
        if not text:
            logger.info(
                "summary_index_skipped_empty_text",
                extra={"correlation_id": correlation_id, "request_id": request_id},
            )
            return

        # task_type="document" + the summary's own ``lang`` (NOT defaulted to
        # "en") match the reconciler's embed call exactly, so the vector is
        # identical. (Only the payload's ``language`` key is "en"-defaulted.)
        embedding = await self._embedding_service.generate_embedding(
            text, language=lang, task_type="document"
        )
        vector: list[float] = (
            embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
        )
        point_payload = build_summary_qdrant_payload(
            summary_id, request_id, lang, payload_dict, scope.user_scope, scope.environment
        )
        # raw_id "{request_id}:{summary_id}" -> str_to_uuid is exactly the
        # shared summary_point_id namespace, so the fast-path and the reconciler
        # converge on one point.
        raw_id = f"{request_id}:{summary_id}"
        acknowledged = await asyncio.to_thread(
            self._vector_store.replace_summary_point,
            request_id,
            raw_id,
            vector,
            point_payload,
        )
        if acknowledged is not True:
            logger.warning(
                "summary_index_qdrant_unacknowledged",
                extra={
                    "correlation_id": correlation_id,
                    "request_id": request_id,
                    "summary_id": summary_id,
                },
            )
            return
        if self._embedding_repository is not None:
            await self._embedding_repository.async_mark_summary_embeddings_indexed([summary_id])
        logger.info(
            "summary_indexed_fastpath",
            extra={
                "correlation_id": correlation_id,
                "request_id": request_id,
                "summary_id": summary_id,
                "point_id": summary_point_id(request_id, summary_id),
            },
        )
