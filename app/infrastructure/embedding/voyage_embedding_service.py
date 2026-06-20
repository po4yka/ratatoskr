"""Embedding service backed by Voyage AI's text embedding API."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import httpx

from app.core.logging_utils import get_logger
from app.infrastructure.embedding.embedding_protocol import EmbeddingSerializationMixin
from app.observability.attributes import EMBEDDING_BATCH_SIZE, EMBEDDING_DIMS, EMBEDDING_MODEL
from app.observability.metrics import record_db_query

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)


def _get_tracer() -> Any:
    from app.observability.otel import get_tracer

    return get_tracer(__name__)


_TASK_TYPE_MAP: dict[str | None, str | None] = {
    "document": "document",
    "query": "query",
    None: None,
}
_BATCH_SIZE = 128
_MAX_CONCURRENT_BATCHES = 4
_MAX_RETRIES = 5
_INITIAL_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 30.0


def _is_rate_limit_error(exc: Exception) -> bool:
    status_code = (
        getattr(exc, "response", None).status_code if getattr(exc, "response", None) else None
    )
    if status_code == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


class VoyageEmbeddingService(EmbeddingSerializationMixin):
    """Generate embeddings via Voyage AI's `/v1/embeddings` endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "voyage-3-large",
        dimensions: int = 1024,
        base_url: str = "https://api.voyageai.com/v1",
        timeout_sec: float = 30.0,
    ) -> None:
        if not api_key:
            msg = "VOYAGE_API_KEY is required when EMBEDDING_PROVIDER=voyage"
            raise ValueError(msg)
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_sec,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            logger.info(
                "voyage_embedding_client_initialized",
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
        del language
        embeddings = await self.generate_embeddings_batch([text], task_type=task_type)
        return embeddings[0]

    async def generate_embeddings_batch(
        self,
        texts: Sequence[str],
        *,
        language: str | None = None,
        task_type: str | None = None,
    ) -> list[list[float]]:
        del language
        if not texts:
            return []

        client = self._ensure_client()
        voyage_task = _TASK_TYPE_MAP.get(task_type)
        chunks = [list(texts[i : i + _BATCH_SIZE]) for i in range(0, len(texts), _BATCH_SIZE)]
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_BATCHES)

        async def _run(chunk: list[str]) -> list[list[float]]:
            async with semaphore:
                return await self._embed_contents_with_retry(client, chunk, voyage_task)

        chunk_embeddings = await asyncio.gather(*(_run(chunk) for chunk in chunks))
        ordered: list[list[float]] = []
        for embeddings in chunk_embeddings:
            ordered.extend(embeddings)
        return ordered

    async def _embed_contents_with_retry(
        self,
        client: httpx.AsyncClient,
        texts: list[str],
        input_type: str | None,
    ) -> list[list[float]]:
        delay = _INITIAL_BACKOFF_SEC
        for attempt in range(_MAX_RETRIES):
            with _get_tracer().start_as_current_span("embedding.voyage_encode") as span:
                span.set_attribute(EMBEDDING_MODEL, self._model)
                span.set_attribute(EMBEDDING_BATCH_SIZE, len(texts))
                span.set_attribute(EMBEDDING_DIMS, self._dimensions)
                t0 = time.monotonic()
                try:
                    response = await client.post(
                        "/embeddings", json=self._payload(texts, input_type)
                    )
                    response.raise_for_status()
                    record_db_query("voyage_embedding", time.monotonic() - t0)
                    return self._parse_embeddings(response.json(), expected_count=len(texts))
                except Exception as exc:
                    record_db_query("voyage_embedding", time.monotonic() - t0)
                    if not _is_rate_limit_error(exc) or attempt == _MAX_RETRIES - 1:
                        raise
                    logger.warning(
                        "voyage_embedding_rate_limited",
                        extra={
                            "attempt": attempt + 1,
                            "retry_in_sec": delay,
                            "batch_size": len(texts),
                        },
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, _MAX_BACKOFF_SEC)
        return []

    def _payload(self, texts: list[str], input_type: str | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "input": texts,
            "model": self._model,
            "output_dimension": self._dimensions,
            "output_dtype": "float",
            "truncation": True,
        }
        if input_type is not None:
            payload["input_type"] = input_type
        return payload

    def _parse_embeddings(self, payload: Any, *, expected_count: int) -> list[list[float]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Voyage embeddings response did not include a data array")

        data = payload["data"]
        if len(data) != expected_count:
            raise ValueError(
                f"Voyage embeddings response returned {len(data)} vectors for {expected_count} inputs"
            )

        if all(isinstance(item, dict) and isinstance(item.get("index"), int) for item in data):
            data = sorted(data, key=lambda item: item["index"])

        embeddings: list[list[float]] = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise ValueError("Voyage embeddings response item did not include an embedding")
            vector = [float(value) for value in item["embedding"]]
            if len(vector) != self._dimensions:
                raise ValueError(
                    "Voyage embeddings response dimension mismatch: "
                    f"expected {self._dimensions}, got {len(vector)}"
                )
            embeddings.append(vector)
        return embeddings

    def get_model_name(self, language: str | None = None) -> str:
        return self._model

    def get_dimensions(self, language: str | None = None) -> int:
        return self._dimensions

    def close(self) -> None:
        """Not supported — VoyageEmbeddingService wraps an httpx.AsyncClient.

        Calling this method would silently drop the client reference without
        draining in-flight connections or closing the underlying transport,
        leaking file descriptors and connection-pool resources.

        Use ``await service.aclose()`` instead.
        """
        msg = "VoyageEmbeddingService.close() is not supported; use await aclose() instead"
        raise TypeError(msg)

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()
