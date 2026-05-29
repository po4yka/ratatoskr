"""Tests for GeminiEmbeddingService."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.infrastructure.embedding.gemini_embedding_service import GeminiEmbeddingService


class TestGeminiEmbeddingServiceInit:
    def test_raises_without_api_key(self) -> None:
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            GeminiEmbeddingService(api_key="")

    def test_defaults(self) -> None:
        svc = GeminiEmbeddingService(api_key="test-key")
        assert svc.get_model_name() == "gemini-embedding-2-preview"
        assert svc.get_dimensions() == 768

    def test_custom_model_and_dims(self) -> None:
        svc = GeminiEmbeddingService(api_key="k", model="custom-model", dimensions=256)
        assert svc.get_model_name() == "custom-model"
        assert svc.get_dimensions() == 256
        assert svc.get_model_name(language="ru") == "custom-model"
        assert svc.get_dimensions(language="en") == 256


class TestGeminiEmbeddingServiceGenerate:
    @pytest.mark.asyncio
    async def test_generate_embedding_calls_api(self) -> None:
        svc = GeminiEmbeddingService(api_key="test-key", dimensions=3)

        fake_embedding = SimpleNamespace(values=[0.1, 0.2, 0.3])
        fake_result = SimpleNamespace(embeddings=[fake_embedding])
        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = fake_result

        with patch("app.infrastructure.embedding.gemini_embedding_service.genai", create=True):
            svc._client = mock_client

            result = await svc.generate_embedding("hello world")

        assert result == [0.1, 0.2, 0.3]
        call_kwargs = mock_client.models.embed_content.call_args
        assert call_kwargs.kwargs["model"] == "gemini-embedding-2-preview"
        assert call_kwargs.kwargs["contents"] == ["hello world"]
        config = call_kwargs.kwargs["config"]
        assert config["task_type"] == "SEMANTIC_SIMILARITY"
        assert config["output_dimensionality"] == 3

    @pytest.mark.asyncio
    async def test_task_type_document(self) -> None:
        svc = GeminiEmbeddingService(api_key="test-key", dimensions=3)
        fake_result = SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 2.0, 3.0])])
        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = fake_result
        svc._client = mock_client

        await svc.generate_embedding("doc text", task_type="document")

        config = mock_client.models.embed_content.call_args.kwargs["config"]
        assert config["task_type"] == "RETRIEVAL_DOCUMENT"

    @pytest.mark.asyncio
    async def test_task_type_query(self) -> None:
        svc = GeminiEmbeddingService(api_key="test-key", dimensions=3)
        fake_result = SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 2.0, 3.0])])
        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = fake_result
        svc._client = mock_client

        await svc.generate_embedding("search query", task_type="query")

        config = mock_client.models.embed_content.call_args.kwargs["config"]
        assert config["task_type"] == "RETRIEVAL_QUERY"


class TestGeminiEmbeddingServiceBatch:
    @pytest.mark.asyncio
    async def test_batch_chunks_into_100_and_preserves_order(self) -> None:
        svc = GeminiEmbeddingService(api_key="test-key", dimensions=1)

        def _embed(**kw: object) -> SimpleNamespace:
            # Echo each input's integer value so order can be verified.
            contents = kw["contents"]
            return SimpleNamespace(
                embeddings=[SimpleNamespace(values=[float(int(c))]) for c in contents]
            )

        mock_client = MagicMock()
        mock_client.models.embed_content.side_effect = _embed
        svc._client = mock_client

        texts = [str(i) for i in range(250)]
        result = await svc.generate_embeddings_batch(texts, task_type="document")

        # ceil(250 / 100) = 3 API calls (not 250 single calls).
        assert mock_client.models.embed_content.call_count == 3
        # Order preserved across chunks.
        assert result == [[float(i)] for i in range(250)]

    @pytest.mark.asyncio
    async def test_batch_retries_on_rate_limit(self) -> None:
        svc = GeminiEmbeddingService(api_key="test-key", dimensions=1)

        class _Rate429(Exception):
            code = 429

        ok = SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0])])
        mock_client = MagicMock()
        mock_client.models.embed_content.side_effect = [_Rate429(), ok]
        svc._client = mock_client

        with patch(
            "app.infrastructure.embedding.gemini_embedding_service.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep_mock:
            result = await svc.generate_embeddings_batch(["only"])

        assert result == [[1.0]]
        assert mock_client.models.embed_content.call_count == 2  # retried once
        sleep_mock.assert_awaited()  # backoff slept

    @pytest.mark.asyncio
    async def test_empty_batch_returns_empty(self) -> None:
        svc = GeminiEmbeddingService(api_key="test-key")
        svc._client = MagicMock()
        assert await svc.generate_embeddings_batch([]) == []


class TestGeminiEmbeddingServiceSerialization:
    def test_serialize_deserialize_roundtrip(self) -> None:
        svc = GeminiEmbeddingService(api_key="test-key")
        original = [0.1, 0.2, 0.3, 0.4]

        blob = svc.serialize_embedding(original)
        restored = svc.deserialize_embedding(blob)

        assert len(restored) == len(original)
        for a, b in zip(original, restored, strict=True):
            assert abs(a - b) < 1e-6

    def test_serialize_numpy_like(self) -> None:
        svc = GeminiEmbeddingService(api_key="test-key")

        class FakeArray:
            def tolist(self):
                return [1.0, 2.0]

        blob = svc.serialize_embedding(FakeArray())
        assert svc.deserialize_embedding(blob) == pytest.approx([1.0, 2.0])


class TestGeminiEmbeddingServiceLifecycle:
    def test_close_clears_client(self) -> None:
        svc = GeminiEmbeddingService(api_key="test-key")
        svc._client = MagicMock()
        svc.close()
        assert svc._client is None

    @pytest.mark.asyncio
    async def test_aclose(self) -> None:
        svc = GeminiEmbeddingService(api_key="test-key")
        svc._client = MagicMock()
        await svc.aclose()
        assert svc._client is None
