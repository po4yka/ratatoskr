"""Embedding service backed by Google Gemini Embedding 2 API."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

from app.core.logging_utils import get_logger
from app.infrastructure.embedding.embedding_protocol import EmbeddingSerializationMixin
from app.observability.attributes import EMBEDDING_BATCH_SIZE, EMBEDDING_DIMS, EMBEDDING_MODEL
from app.observability.metrics import record_db_query

logger = get_logger(__name__)


def _get_tracer() -> Any:
    from app.observability.otel import get_tracer

    return get_tracer(__name__)


# Task type mapping: caller-friendly names -> Gemini API enum values
_TASK_TYPE_MAP: dict[str | None, str] = {
    "document": "RETRIEVAL_DOCUMENT",
    "query": "RETRIEVAL_QUERY",
    None: "SEMANTIC_SIMILARITY",
}

# Gemini embed_content accepts up to 100 inputs per request.
_BATCH_SIZE = 100
# Concurrent in-flight batch requests (bounds the 429 pressure during backfill).
_MAX_CONCURRENT_BATCHES = 4
# Exponential backoff on rate-limit (429 / RESOURCE_EXHAUSTED) responses.
_MAX_RETRIES = 5
_INITIAL_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 30.0


def _is_rate_limit_error(exc: Exception) -> bool:
    """Best-effort detection of a Gemini rate-limit / quota error."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429:
        return True
    text = f"{getattr(exc, 'status', '')} {exc}".lower()
    return "429" in text or "resource_exhausted" in text or "rate limit" in text


class GeminiEmbeddingService(EmbeddingSerializationMixin):
    """Generate embeddings via Google Gemini Embedding API.

    Uses lazy import of ``google.genai`` so the app works without the
    dependency when ``EMBEDDING_PROVIDER=local``.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-embedding-2-preview",
        dimensions: int = 768,
    ) -> None:
        if not api_key:
            msg = "GEMINI_API_KEY is required when EMBEDDING_PROVIDER=gemini"
            raise ValueError(msg)
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._client: Any | None = None

    def _ensure_client(self) -> Any:
        """Lazily initialise the google-genai client."""
        if self._client is None:
            from importlib import import_module

            genai = import_module("google.genai")
            self._client = genai.Client(api_key=self._api_key)
            logger.info(
                "gemini_embedding_client_initialized",
                extra={"model": self._model, "dimensions": self._dimensions},
            )
        return self._client

    async def generate_embedding(
        self,
        text: str,
        *,
        language: str | None = None,
        task_type: str | None = None,
    ) -> list[float]:
        """Generate embedding via Gemini API.

        Args:
            text: Text to embed.
            language: Ignored (Gemini is natively multilingual).
            task_type: One of ``"document"``, ``"query"``, or ``None``.
        """
        client = self._ensure_client()
        gemini_task = _TASK_TYPE_MAP.get(task_type, "SEMANTIC_SIMILARITY")
        embeddings = await self._embed_contents_with_retry(client, [text], gemini_task)
        return embeddings[0]

    async def generate_embeddings_batch(
        self,
        texts: Sequence[str],
        *,
        language: str | None = None,
        task_type: str | None = None,
    ) -> list[Any]:
        """Batch embedding via Gemini's multi-input embed_content.

        Chunks into <=100 inputs per request (Gemini's batch limit), runs at most
        _MAX_CONCURRENT_BATCHES requests concurrently, retries rate-limit
        responses with exponential backoff, and preserves input order.
        """
        if not texts:
            return []
        client = self._ensure_client()
        gemini_task = _TASK_TYPE_MAP.get(task_type, "SEMANTIC_SIMILARITY")
        chunks = [list(texts[i : i + _BATCH_SIZE]) for i in range(0, len(texts), _BATCH_SIZE)]
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_BATCHES)

        async def _run(chunk: list[str]) -> list[Any]:
            async with semaphore:
                return await self._embed_contents_with_retry(client, chunk, gemini_task)

        # gather preserves chunk order; each chunk preserves input order.
        chunk_embeddings = await asyncio.gather(*(_run(chunk) for chunk in chunks))
        ordered: list[Any] = []
        for embeddings in chunk_embeddings:
            ordered.extend(embeddings)
        return ordered

    async def _embed_contents_with_retry(
        self,
        client: Any,
        contents: list[str],
        gemini_task: str,
    ) -> list[list[float]]:
        """Embed up to _BATCH_SIZE inputs in one call, retrying on rate limits."""
        delay = _INITIAL_BACKOFF_SEC
        for attempt in range(_MAX_RETRIES):
            with _get_tracer().start_as_current_span("embedding.gemini_encode") as span:
                span.set_attribute(EMBEDDING_MODEL, self._model)
                span.set_attribute(EMBEDDING_BATCH_SIZE, len(contents))
                span.set_attribute(EMBEDDING_DIMS, self._dimensions)
                t0 = time.monotonic()
                try:
                    result = await asyncio.to_thread(
                        client.models.embed_content,
                        model=self._model,
                        contents=contents,
                        config={
                            "task_type": gemini_task,
                            "output_dimensionality": self._dimensions,
                        },
                    )
                    record_db_query("gemini_embedding", time.monotonic() - t0)
                    return [embedding.values for embedding in result.embeddings]
                except Exception as exc:
                    record_db_query("gemini_embedding", time.monotonic() - t0)
                    if not _is_rate_limit_error(exc) or attempt == _MAX_RETRIES - 1:
                        raise
                    logger.warning(
                        "gemini_embedding_rate_limited",
                        extra={
                            "attempt": attempt + 1,
                            "retry_in_sec": delay,
                            "batch_size": len(contents),
                        },
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, _MAX_BACKOFF_SEC)
        return []  # unreachable: last attempt either returns or raises

    # -- Metadata --------------------------------------------------------------

    def get_model_name(self, language: str | None = None) -> str:
        return self._model

    def get_dimensions(self, language: str | None = None) -> int:
        return self._dimensions

    # -- Lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self._client = None

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)
