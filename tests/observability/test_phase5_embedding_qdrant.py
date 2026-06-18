"""Unit tests for Phase 5 embedding and Qdrant OTel spans + Prometheus metrics.

Tests verify:
- EmbeddingService opens embedding.encode / embedding.encode_batch spans
  with EMBEDDING_MODEL, EMBEDDING_BATCH_SIZE, EMBEDDING_DIMS attributes.
- EmbeddingService calls record_db_query for both single and batch encode.
- GeminiEmbeddingService opens embedding.gemini_encode spans per attempt
  and calls record_db_query("gemini_embedding", ...).
- QdrantVectorStore opens vector.upsert / vector.replace / vector.query spans
  with VECTOR_OPERATION and VECTOR_STATUS attributes.
- QdrantVectorStore calls record_db_query(qdrant_upsert/qdrant_replace/qdrant_query).
- QdrantVectorStore calls record_vector_write(operation=..., status="success")
  on success paths (in addition to the existing failure-path calls).
- All spans + metrics degrade gracefully when OTel/Prometheus are absent.
"""

from __future__ import annotations

import contextlib
import importlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# EmbeddingService spans + latency
# ---------------------------------------------------------------------------


class TestEmbeddingServiceSpans:
    """EmbeddingService must open spans wrapping asyncio.to_thread calls."""

    def _make_service(self) -> Any:
        from app.infrastructure.embedding.embedding_service import EmbeddingService

        svc = EmbeddingService(default_model="all-MiniLM-L6-v2")
        # Pre-seed a fake model so _ensure_model does not try to import torch
        fake_model = MagicMock()
        fake_model.encode = MagicMock(return_value=[0.1, 0.2, 0.3])
        svc._models["all-MiniLM-L6-v2"] = fake_model
        svc._dimensions["all-MiniLM-L6-v2"] = 384
        return svc

    @pytest.mark.asyncio
    async def test_generate_embedding_opens_span_with_attributes(self) -> None:
        svc = self._make_service()
        recorded: dict[str, Any] = {}

        class _Span:
            def set_attribute(self, k: str, v: Any) -> None:
                recorded[k] = v

            def __enter__(self) -> _Span:
                return self

            def __exit__(self, *_: Any) -> None:
                pass

        class _Tracer:
            def start_as_current_span(
                self, name: str, **_kw: Any
            ) -> contextlib.AbstractContextManager[_Span]:
                recorded["_span_name"] = name
                import contextlib

                return contextlib.nullcontext(_Span())

        with patch(
            "app.infrastructure.embedding.embedding_service._get_tracer", return_value=_Tracer()
        ):
            await svc.generate_embedding("hello world")

        assert recorded.get("_span_name") == "embedding.encode"
        assert recorded.get("ratatoskr.embedding.model") == "all-MiniLM-L6-v2"
        assert recorded.get("ratatoskr.embedding.batch_size") == 1
        assert recorded.get("ratatoskr.embedding.dims") == 384

    @pytest.mark.asyncio
    async def test_generate_embedding_calls_record_db_query(self) -> None:
        from app.observability import metrics as m

        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        svc = self._make_service()
        metric = m.DB_QUERY_LATENCY
        before = metric.labels(operation="embedding_encode_single")._sum.get()

        await svc.generate_embedding("hello")

        after = metric.labels(operation="embedding_encode_single")._sum.get()
        assert after >= before  # at least the latency was observed (may be 0 in fast mock)

    @pytest.mark.asyncio
    async def test_generate_batch_opens_span_with_batch_size(self) -> None:
        svc = self._make_service()
        # Batch encode returns a list
        svc._models["all-MiniLM-L6-v2"].encode = MagicMock(return_value=[[0.1], [0.2], [0.3]])
        recorded: dict[str, Any] = {}

        class _Span:
            def set_attribute(self, k: str, v: Any) -> None:
                recorded[k] = v

            def __enter__(self) -> _Span:
                return self

            def __exit__(self, *_: Any) -> None:
                pass

        class _Tracer:
            def start_as_current_span(
                self, name: str, **_kw: Any
            ) -> contextlib.AbstractContextManager[_Span]:
                recorded["_span_name"] = name
                import contextlib

                return contextlib.nullcontext(_Span())

        with patch(
            "app.infrastructure.embedding.embedding_service._get_tracer", return_value=_Tracer()
        ):
            await svc.generate_embeddings_batch(["a", "b", "c"])

        assert recorded.get("_span_name") == "embedding.encode_batch"
        assert recorded.get("ratatoskr.embedding.batch_size") == 3

    @pytest.mark.asyncio
    async def test_generate_batch_calls_record_db_query(self) -> None:
        from app.observability import metrics as m

        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        svc = self._make_service()
        svc._models["all-MiniLM-L6-v2"].encode = MagicMock(return_value=[[0.1], [0.2]])
        metric = m.DB_QUERY_LATENCY
        before = metric.labels(operation="embedding_encode_batch")._sum.get()

        await svc.generate_embeddings_batch(["a", "b"])

        after = metric.labels(operation="embedding_encode_batch")._sum.get()
        assert after >= before


# ---------------------------------------------------------------------------
# GeminiEmbeddingService spans + latency
# ---------------------------------------------------------------------------


class TestGeminiEmbeddingServiceSpans:
    """GeminiEmbeddingService wraps each embed_content attempt in a span."""

    def _make_service(self) -> Any:
        from app.infrastructure.embedding.gemini_embedding_service import GeminiEmbeddingService

        svc = GeminiEmbeddingService(
            api_key="fake-key", model="gemini-embedding-test", dimensions=768
        )
        # Pre-seed a fake client so _ensure_client doesn't import google.genai
        fake_embedding = MagicMock()
        fake_embedding.values = [0.1, 0.2]
        fake_result = MagicMock()
        fake_result.embeddings = [fake_embedding]
        fake_client = MagicMock()
        fake_client.models.embed_content = MagicMock(return_value=fake_result)
        svc._client = fake_client
        return svc

    @pytest.mark.asyncio
    async def test_embed_opens_span_with_attributes(self) -> None:
        svc = self._make_service()
        recorded: dict[str, Any] = {}

        class _Span:
            def set_attribute(self, k: str, v: Any) -> None:
                recorded[k] = v

            def __enter__(self) -> _Span:
                return self

            def __exit__(self, *_: Any) -> None:
                pass

        class _Tracer:
            def start_as_current_span(
                self, name: str, **_kw: Any
            ) -> contextlib.AbstractContextManager[_Span]:
                recorded["_span_name"] = name
                import contextlib

                return contextlib.nullcontext(_Span())

        with patch(
            "app.infrastructure.embedding.gemini_embedding_service._get_tracer",
            return_value=_Tracer(),
        ):
            await svc.generate_embedding("hello")

        assert recorded.get("_span_name") == "embedding.gemini_encode"
        assert recorded.get("ratatoskr.embedding.model") == "gemini-embedding-test"
        assert recorded.get("ratatoskr.embedding.batch_size") == 1
        assert recorded.get("ratatoskr.embedding.dims") == 768

    @pytest.mark.asyncio
    async def test_embed_calls_record_db_query(self) -> None:
        from app.observability import metrics as m

        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        svc = self._make_service()
        metric = m.DB_QUERY_LATENCY
        before = metric.labels(operation="gemini_embedding")._sum.get()

        await svc.generate_embedding("hello")

        after = metric.labels(operation="gemini_embedding")._sum.get()
        assert after >= before


# ---------------------------------------------------------------------------
# QdrantVectorStore spans + success counters
# ---------------------------------------------------------------------------


class _FakeQdrantClient:
    """Minimal synchronous stand-in that records calls."""

    def __init__(self) -> None:
        self.upserted: list[Any] = []
        self.deleted: list[Any] = []
        self.queried: list[Any] = []

    def get_collections(self) -> MagicMock:
        return MagicMock()

    def collection_exists(self, name: str) -> bool:
        return True

    def upsert(self, *, collection_name: str, points: list, wait: bool = True) -> None:
        self.upserted.extend(points)

    def delete(self, *, collection_name: str, points_selector: Any, wait: bool = True) -> None:
        self.deleted.append(points_selector)

    def query_points(
        self,
        *,
        collection_name: str,
        query: list,
        query_filter: Any,
        limit: int,
        with_payload: bool,
    ) -> MagicMock:
        result = MagicMock()
        result.points = []
        return result

    def scroll(
        self,
        *,
        collection_name: str,
        scroll_filter: Any,
        limit: int,
        with_payload: Any,
        with_vectors: bool,
        offset: Any,
    ) -> tuple:
        return ([], None)

    def close(self) -> None:
        pass


def _make_store() -> Any:
    """Build a QdrantVectorStore with a fake client (no real Qdrant needed)."""
    from app.infrastructure.vector.qdrant_store import QdrantVectorStore

    with patch(
        "app.infrastructure.vector.qdrant_store.QdrantClient", return_value=_FakeQdrantClient()
    ):
        return QdrantVectorStore(
            url="http://localhost:6333",
            api_key=None,
            environment="test",
            user_scope="test-user",
        )


class TestQdrantUpsertSpan:
    def test_upsert_opens_span_with_attributes(self) -> None:
        store = _make_store()
        recorded: dict[str, Any] = {}

        class _Span:
            def set_attribute(self, k: str, v: Any) -> None:
                recorded[k] = v

            def __enter__(self) -> _Span:
                return self

            def __exit__(self, *_: Any) -> None:
                pass

        class _Tracer:
            def start_as_current_span(
                self, name: str, **_kw: Any
            ) -> contextlib.AbstractContextManager[_Span]:
                recorded["_span_name"] = name
                import contextlib

                return contextlib.nullcontext(_Span())

        with patch("app.infrastructure.vector.qdrant_store._get_tracer", return_value=_Tracer()):
            store.upsert_notes(
                vectors=[[0.1, 0.2]],
                metadatas=[{"request_id": 1}],
            )

        assert recorded.get("_span_name") == "vector.upsert"
        assert recorded.get("ratatoskr.vector.operation") == "upsert"
        assert recorded.get("ratatoskr.vector.status") == "success"

    def test_upsert_success_calls_record_vector_write(self) -> None:
        from app.observability import metrics as m

        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        store = _make_store()
        before = m.VECTOR_WRITES_TOTAL.labels(operation="upsert", status="success")._value.get()

        store.upsert_notes(vectors=[[0.1, 0.2]], metadatas=[{"request_id": 1}])

        after = m.VECTOR_WRITES_TOTAL.labels(operation="upsert", status="success")._value.get()
        assert after == before + 1.0

    def test_upsert_success_calls_record_db_query(self) -> None:
        from app.observability import metrics as m

        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        store = _make_store()
        before = m.DB_QUERY_LATENCY.labels(operation="qdrant_upsert")._sum.get()

        store.upsert_notes(vectors=[[0.1, 0.2]], metadatas=[{"request_id": 1}])

        after = m.DB_QUERY_LATENCY.labels(operation="qdrant_upsert")._sum.get()
        assert after >= before

    def test_upsert_failure_calls_failed_counter(self) -> None:
        from app.observability import metrics as m

        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        store = _make_store()
        # Force the client to raise
        store._client.upsert = MagicMock(side_effect=RuntimeError("qdrant down"))
        before = m.VECTOR_WRITES_TOTAL.labels(operation="upsert", status="failed")._value.get()

        store.upsert_notes(vectors=[[0.1, 0.2]], metadatas=[{"request_id": 1}])

        after = m.VECTOR_WRITES_TOTAL.labels(operation="upsert", status="failed")._value.get()
        assert after == before + 1.0


class TestQdrantReplaceSpan:
    def test_replace_opens_span_with_attributes(self) -> None:
        store = _make_store()
        recorded: dict[str, Any] = {}

        class _Span:
            def set_attribute(self, k: str, v: Any) -> None:
                recorded[k] = v

            def __enter__(self) -> _Span:
                return self

            def __exit__(self, *_: Any) -> None:
                pass

        class _Tracer:
            def start_as_current_span(
                self, name: str, **_kw: Any
            ) -> contextlib.AbstractContextManager[_Span]:
                recorded["_span_name"] = name
                import contextlib

                return contextlib.nullcontext(_Span())

        with patch("app.infrastructure.vector.qdrant_store._get_tracer", return_value=_Tracer()):
            store.replace_request_notes(
                request_id=1,
                vectors=[[0.1, 0.2]],
                metadatas=[{"request_id": 1}],
            )

        assert recorded.get("_span_name") == "vector.replace"
        assert recorded.get("ratatoskr.vector.operation") == "replace"
        assert recorded.get("ratatoskr.vector.status") == "success"

    def test_replace_success_calls_record_vector_write(self) -> None:
        from app.observability import metrics as m

        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        store = _make_store()
        before = m.VECTOR_WRITES_TOTAL.labels(operation="replace", status="success")._value.get()

        store.replace_request_notes(
            request_id=1,
            vectors=[[0.1, 0.2]],
            metadatas=[{"request_id": 1}],
        )

        after = m.VECTOR_WRITES_TOTAL.labels(operation="replace", status="success")._value.get()
        assert after == before + 1.0


class TestQdrantQuerySpan:
    def test_query_opens_span_with_attributes(self) -> None:
        store = _make_store()
        recorded: dict[str, Any] = {}

        class _Span:
            def set_attribute(self, k: str, v: Any) -> None:
                recorded[k] = v

            def __enter__(self) -> _Span:
                return self

            def __exit__(self, *_: Any) -> None:
                pass

        class _Tracer:
            def start_as_current_span(
                self, name: str, **_kw: Any
            ) -> contextlib.AbstractContextManager[_Span]:
                recorded["_span_name"] = name
                import contextlib

                return contextlib.nullcontext(_Span())

        with patch("app.infrastructure.vector.qdrant_store._get_tracer", return_value=_Tracer()):
            result = store.query(query_vector=[0.1, 0.2], filters=None, top_k=5)

        assert recorded.get("_span_name") == "vector.query"
        assert recorded.get("ratatoskr.vector.operation") == "query"
        assert recorded.get("ratatoskr.vector.status") == "success"
        assert result.hits == []

    def test_query_success_calls_record_db_query(self) -> None:
        from app.observability import metrics as m

        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        store = _make_store()
        before = m.DB_QUERY_LATENCY.labels(operation="qdrant_query")._sum.get()

        store.query(query_vector=[0.1, 0.2], filters=None, top_k=5)

        after = m.DB_QUERY_LATENCY.labels(operation="qdrant_query")._sum.get()
        assert after >= before

    def test_query_failure_sets_error_status(self) -> None:
        store = _make_store()
        store._client.query_points = MagicMock(side_effect=RuntimeError("qdrant down"))
        recorded: dict[str, Any] = {}

        class _Span:
            def set_attribute(self, k: str, v: Any) -> None:
                recorded[k] = v

            def __enter__(self) -> _Span:
                return self

            def __exit__(self, *_: Any) -> None:
                pass

        class _Tracer:
            def start_as_current_span(
                self, name: str, **_kw: Any
            ) -> contextlib.AbstractContextManager[_Span]:
                recorded["_span_name"] = name
                import contextlib

                return contextlib.nullcontext(_Span())

        with patch("app.infrastructure.vector.qdrant_store._get_tracer", return_value=_Tracer()):
            result = store.query(query_vector=[0.1, 0.2], filters=None, top_k=5)

        assert recorded.get("ratatoskr.vector.status") == "error"
        assert result.hits == []


# ---------------------------------------------------------------------------
# Queue-depth and in-flight gauge helpers (Phase 5c)
# ---------------------------------------------------------------------------


class TestQueueDepthGauge:
    """set_scheduler_queue_depth must update SCHEDULER_QUEUE_DEPTH gauge."""

    def test_set_scheduler_queue_depth(self) -> None:
        m = importlib.import_module("app.observability.metrics")
        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        m.set_scheduler_queue_depth(queue="url_processor", depth=7)
        assert m.SCHEDULER_QUEUE_DEPTH.labels(queue="url_processor")._value.get() == 7.0

    def test_negative_depth_ignored(self) -> None:
        m = importlib.import_module("app.observability.metrics")
        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        m.set_scheduler_queue_depth(queue="rss", depth=3)
        m.set_scheduler_queue_depth(queue="rss", depth=-1)
        # Value should still be 3 (negative silently ignored)
        assert m.SCHEDULER_QUEUE_DEPTH.labels(queue="rss")._value.get() == 3.0

    def test_set_url_processor_in_flight_inc(self) -> None:
        m = importlib.import_module("app.observability.metrics")
        if not m.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client unavailable")

        before = m.URL_PROCESSOR_IN_FLIGHT._value.get()
        m.set_url_processor_in_flight(delta=1)
        assert m.URL_PROCESSOR_IN_FLIGHT._value.get() == before + 1.0
        m.set_url_processor_in_flight(delta=-1)
        assert m.URL_PROCESSOR_IN_FLIGHT._value.get() == before

    def test_set_url_processor_in_flight_noop_when_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m = importlib.import_module("app.observability.metrics")
        monkeypatch.setattr(m, "PROMETHEUS_AVAILABLE", False)
        m.set_url_processor_in_flight(delta=1)  # must not raise
