"""Tests for VoyageEmbeddingService."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.application.services.summary_embedding_generator import SummaryEmbeddingGenerator
from app.infrastructure.embedding.embedding_protocol import unpack_embedding
from app.infrastructure.embedding.voyage_embedding_service import VoyageEmbeddingService


class TestVoyageEmbeddingServiceInit:
    def test_raises_without_api_key(self) -> None:
        with pytest.raises(ValueError, match="VOYAGE_API_KEY"):
            VoyageEmbeddingService(api_key="")

    def test_defaults(self) -> None:
        svc = VoyageEmbeddingService(api_key="test-key")
        assert svc.get_model_name() == "voyage-3-large"
        assert svc.get_dimensions() == 1024


class TestVoyageEmbeddingServiceGenerate:
    @pytest.mark.asyncio
    async def test_generate_embedding_calls_api(self) -> None:
        requests: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(
                {
                    "url": str(request.url),
                    "authorization": request.headers.get("authorization"),
                    "json": dict(request.read() and __import__("json").loads(request.content)),
                }
            )
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
            )

        transport = httpx.MockTransport(handler)
        svc = VoyageEmbeddingService(api_key="test-key", dimensions=3)
        svc._client = httpx.AsyncClient(
            base_url="https://api.voyageai.com/v1",
            transport=transport,
            headers={"Authorization": "Bearer test-key"},
        )

        result = await svc.generate_embedding("hello world", task_type="document")
        await svc.aclose()

        assert result == [0.1, 0.2, 0.3]
        assert requests == [
            {
                "url": "https://api.voyageai.com/v1/embeddings",
                "authorization": "Bearer test-key",
                "json": {
                    "input": ["hello world"],
                    "model": "voyage-3-large",
                    "output_dimension": 3,
                    "output_dtype": "float",
                    "truncation": True,
                    "input_type": "document",
                },
            }
        ]

    @pytest.mark.asyncio
    async def test_batch_preserves_response_index_order(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [2.0]},
                        {"index": 0, "embedding": [1.0]},
                    ]
                },
            )

        svc = VoyageEmbeddingService(api_key="test-key", dimensions=1)
        svc._client = httpx.AsyncClient(
            base_url="https://api.voyageai.com/v1",
            transport=httpx.MockTransport(handler),
        )

        result = await svc.generate_embeddings_batch(["a", "b"], task_type="query")
        await svc.aclose()

        assert result == [[1.0], [2.0]]

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit(self) -> None:
        calls = {"count": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if calls["count"] == 1:
                return httpx.Response(429, json={"detail": "rate limit"})
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

        svc = VoyageEmbeddingService(api_key="test-key", dimensions=1)
        svc._client = httpx.AsyncClient(
            base_url="https://api.voyageai.com/v1",
            transport=httpx.MockTransport(handler),
        )

        with patch(
            "app.infrastructure.embedding.voyage_embedding_service.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep_mock:
            result = await svc.generate_embeddings_batch(["only"])
        await svc.aclose()

        assert result == [[1.0]]
        assert calls["count"] == 2
        sleep_mock.assert_awaited()

    @pytest.mark.asyncio
    async def test_dimension_mismatch_raises(self) -> None:
        svc = VoyageEmbeddingService(api_key="test-key", dimensions=3)
        svc._client = httpx.AsyncClient(
            base_url="https://api.voyageai.com/v1",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]},
                )
            ),
        )

        with pytest.raises(ValueError, match="dimension mismatch"):
            await svc.generate_embedding("bad")
        await svc.aclose()


@pytest.mark.asyncio
async def test_voyage_provider_drives_summary_embedding_generator() -> None:
    captured_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
        )

    class _EmbeddingRepo:
        def __init__(self) -> None:
            self.created: dict[str, Any] | None = None

        async def async_get_summary_embedding(self, _summary_id: int) -> None:
            return None

        async def async_create_or_update_summary_embedding(self, **kwargs: Any) -> None:
            self.created = kwargs

    embedding_repo = _EmbeddingRepo()
    service = VoyageEmbeddingService(api_key="test-key", dimensions=3)
    service._client = httpx.AsyncClient(
        base_url="https://api.voyageai.com/v1",
        transport=httpx.MockTransport(handler),
    )
    generator = SummaryEmbeddingGenerator(
        embedding_repository=embedding_repo,  # type: ignore[arg-type]
        request_repository=object(),  # type: ignore[arg-type]
        summary_repository=object(),  # type: ignore[arg-type]
        embedding_service=service,
    )

    created = await generator.generate_embedding_for_summary(
        101,
        {"summary_250": "A concise summary about vector search."},
        language="en",
    )
    await service.aclose()

    assert created is True
    assert captured_payloads[0]["input_type"] == "document"
    assert captured_payloads[0]["model"] == "voyage-3-large"
    assert embedding_repo.created is not None
    assert embedding_repo.created["summary_id"] == 101
    assert embedding_repo.created["model_name"] == "voyage-3-large"
    assert embedding_repo.created["dimensions"] == 3
    assert unpack_embedding(embedding_repo.created["embedding_blob"]) == pytest.approx(
        [0.1, 0.2, 0.3]
    )
