"""Application service for generating embeddings for summaries."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.application.ports.search import EmbeddingDependencyUnavailableError
from app.core.embedding_text import prepare_text_for_embedding
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.application.ports.requests import RequestRepositoryPort
    from app.application.ports.search import EmbeddingProviderPort, EmbeddingRepositoryPort
    from app.application.ports.summaries import SummaryRepositoryPort

logger = get_logger(__name__)


@dataclass
class EmbeddingBatchResult:
    """Per-batch tally returned by :meth:`generate_embeddings_for_summaries`."""

    indexed: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass
class _PreparedRow:
    """A summary whose text is ready to be embedded."""

    summary_id: int
    text: str
    model_name: str
    content_hash: str


class SummaryEmbeddingGenerator:
    """Generates and stores embeddings for article summaries."""

    def __init__(
        self,
        *,
        embedding_repository: EmbeddingRepositoryPort,
        request_repository: RequestRepositoryPort,
        summary_repository: SummaryRepositoryPort,
        embedding_service: EmbeddingProviderPort,
        model_version: str = "1.0",
        max_token_length: int = 512,
    ) -> None:
        self.embedding_repo = embedding_repository
        self.request_repo = request_repository
        self.summary_repo = summary_repository
        self._embedding_service = embedding_service
        self._model_version = model_version
        self._max_token_length = max_token_length
        # The embedding backend being unavailable (torch/CUDA libs missing) is a
        # process-wide condition, not a per-summary one -- warn once, then stay
        # quiet so a reconcile batch does not emit one record per row.
        self._dependency_warning_logged = False

    @property
    def embedding_service(self) -> EmbeddingProviderPort:
        """Expose the embedding provider in use."""
        return self._embedding_service

    async def generate_embedding_for_summary(
        self,
        summary_id: int,
        payload: dict[str, Any],
        *,
        language: str | None = None,
        force: bool = False,
    ) -> bool:
        """Generate and store an embedding for a specific summary."""
        model_name = self._embedding_service.get_model_name(language)
        metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
        text = prepare_text_for_embedding(
            title=metadata.get("title") or payload.get("title"),
            summary_1000=payload.get("summary_1000"),
            summary_250=payload.get("summary_250"),
            tldr=payload.get("tldr"),
            key_ideas=payload.get("key_ideas"),
            topic_tags=payload.get("topic_tags"),
            semantic_boosters=payload.get("semantic_boosters"),
            query_expansion_keywords=payload.get("query_expansion_keywords"),
            semantic_chunks=payload.get("semantic_chunks"),
            max_length=self._max_token_length,
        )
        if not text.strip():
            logger.warning("empty_text_for_embedding", extra={"summary_id": summary_id})
            return False

        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if not force:
            existing = await self.embedding_repo.async_get_summary_embedding(summary_id)
            if existing and existing.get("model_name") == model_name:
                if existing.get("content_hash") == content_hash:
                    logger.debug(
                        "embedding_already_indexed",
                        extra={
                            "summary_id": summary_id,
                            "model": model_name,
                            "language": language,
                        },
                    )
                    return False
                # Same model but stale text — fall through to re-embed.

        try:
            embedding = await self._embedding_service.generate_embedding(
                text,
                language=language,
                task_type="document",
            )
            await self.embedding_repo.async_create_or_update_summary_embedding(
                summary_id=summary_id,
                embedding_blob=self._embedding_service.serialize_embedding(embedding),
                model_name=model_name,
                model_version=self._model_version,
                dimensions=len(embedding),
                language=language,
                content_hash=content_hash,
            )
            logger.info(
                "embedding_generated",
                extra={
                    "summary_id": summary_id,
                    "model": model_name,
                    "language": language,
                    "dimensions": len(embedding),
                    "text_length": len(text),
                },
            )
            return True
        except EmbeddingDependencyUnavailableError as exc:
            # Hard environment outage (e.g. torch/CUDA libs missing): no
            # traceback, and only one warning for the whole process lifetime.
            if not self._dependency_warning_logged:
                logger.warning(
                    "embedding_skipped_dependency_unavailable",
                    extra={"summary_id": summary_id, "detail": str(exc)},
                )
                self._dependency_warning_logged = True
            else:
                logger.debug(
                    "embedding_skipped_dependency_unavailable",
                    extra={"summary_id": summary_id},
                )
            return False
        except (RuntimeError, ValueError, OSError, TypeError):
            logger.exception(
                "embedding_generation_failed",
                extra={"summary_id": summary_id, "language": language},
            )
            return False

    def _prepare_row(
        self,
        summary_id: int,
        payload: Any,
        language: str | None,
    ) -> tuple[_PreparedRow | None, bool]:
        """Build a prepared row for embedding.

        Returns ``(row, skip)``: ``row`` is ``None`` when the summary has no
        embeddable text (``skip`` True), otherwise the prepared row.
        """
        if not isinstance(payload, dict):
            return None, True
        model_name = self._embedding_service.get_model_name(language)
        metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
        text = prepare_text_for_embedding(
            title=metadata.get("title") or payload.get("title"),
            summary_1000=payload.get("summary_1000"),
            summary_250=payload.get("summary_250"),
            tldr=payload.get("tldr"),
            key_ideas=payload.get("key_ideas"),
            topic_tags=payload.get("topic_tags"),
            semantic_boosters=payload.get("semantic_boosters"),
            query_expansion_keywords=payload.get("query_expansion_keywords"),
            semantic_chunks=payload.get("semantic_chunks"),
            max_length=self._max_token_length,
        )
        if not text.strip():
            logger.warning("empty_text_for_embedding", extra={"summary_id": summary_id})
            return None, True
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return _PreparedRow(summary_id, text, model_name, content_hash), False

    async def generate_embeddings_for_summaries(
        self,
        items: Sequence[tuple[int, Any, str | None]],
        *,
        force: bool = False,
    ) -> EmbeddingBatchResult:
        """Embed many summaries with a single batched ``encode`` per language.

        Each tuple is ``(summary_id, payload, language)``. Rows are grouped by
        language (model) so the underlying provider runs one native batch per
        model instead of one ``model.encode`` call per row -- the dominant cost
        on the reconcile path. Returns a tally of indexed/skipped/failed rows.
        """
        result = EmbeddingBatchResult()
        by_language: dict[str | None, list[_PreparedRow]] = defaultdict(list)

        for summary_id, payload, language in items:
            row, skip = self._prepare_row(summary_id, payload, language)
            if skip or row is None:
                result.skipped += 1
                continue
            if not force:
                existing = await self.embedding_repo.async_get_summary_embedding(summary_id)
                if (
                    existing
                    and existing.get("model_name") == row.model_name
                    and existing.get("content_hash") == row.content_hash
                ):
                    result.skipped += 1
                    continue
            by_language[language].append(row)

        for language, rows in by_language.items():
            try:
                embeddings = await self._embedding_service.generate_embeddings_batch(
                    [r.text for r in rows],
                    language=language,
                    task_type="document",
                )
            except EmbeddingDependencyUnavailableError as exc:
                if not self._dependency_warning_logged:
                    logger.warning(
                        "embedding_skipped_dependency_unavailable",
                        extra={"detail": str(exc), "count": len(rows)},
                    )
                    self._dependency_warning_logged = True
                result.skipped += len(rows)
                continue
            except (RuntimeError, ValueError, OSError, TypeError):
                logger.exception(
                    "embedding_batch_generation_failed",
                    extra={"language": language, "count": len(rows)},
                )
                result.failed += len(rows)
                continue

            for row, embedding in zip(rows, embeddings, strict=True):
                try:
                    await self.embedding_repo.async_create_or_update_summary_embedding(
                        summary_id=row.summary_id,
                        embedding_blob=self._embedding_service.serialize_embedding(embedding),
                        model_name=row.model_name,
                        model_version=self._model_version,
                        dimensions=len(embedding),
                        language=language,
                        content_hash=row.content_hash,
                    )
                    result.indexed += 1
                except (RuntimeError, ValueError, OSError, TypeError):
                    logger.exception(
                        "embedding_persist_failed",
                        extra={"summary_id": row.summary_id},
                    )
                    result.failed += 1
        return result

    async def generate_embedding_for_request(self, request_id: int, *, force: bool = False) -> bool:
        """Generate an embedding for the summary produced by a request."""
        request = await self.request_repo.async_get_request_by_id(request_id)
        if not request:
            logger.warning("no_request_found", extra={"request_id": request_id})
            return False

        summary = await self.summary_repo.async_get_summary_by_request(request_id)
        if not summary:
            logger.warning("no_summary_for_request", extra={"request_id": request_id})
            return False

        summary_id = summary.get("id")
        payload = summary.get("json_payload")
        if not summary_id or not isinstance(payload, dict):
            logger.warning(
                "invalid_summary_data",
                extra={"request_id": request_id, "summary_id": summary_id},
            )
            return False

        return await self.generate_embedding_for_summary(
            summary_id=summary_id,
            payload=payload,
            language=request.get("lang_detected"),
            force=force,
        )
