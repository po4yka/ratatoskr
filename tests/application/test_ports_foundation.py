"""T3 ports-and-adapters foundation: structural conformance checks.

Each new application port is ``@runtime_checkable`` and structurally satisfied
by its intended adapter (ADR-0014). Where the real adapter lands in a later
track (retrieval -> T4, extraction -> T7), a minimal structural stand-in
documents the surface the future adapter must meet.
"""

from __future__ import annotations

from typing import Any

from app.application.dto.vector_search import EntityType, RetrievalHit, RetrievalResult
from app.application.ports import ExtractionPort, RetrievalPort, StreamSinkPort


def test_new_ports_are_runtime_checkable() -> None:
    for port in (RetrievalPort, ExtractionPort, StreamSinkPort):
        assert getattr(port, "_is_runtime_protocol", False) is True


def test_stream_hub_adapter_satisfies_stream_sink_port() -> None:
    # T8 finalized the StreamSink surface (stage/section/warning/done/error);
    # the StreamHubStreamSink adapter is now the structural implementation that
    # wraps the raw StreamHub.publish target (ADR-0017).
    from app.adapters.content.streaming.stream_sink_hub import StreamHubStreamSink

    assert isinstance(StreamHubStreamSink(), StreamSinkPort)


def test_retrieval_adapter_shape_satisfies_port() -> None:
    class _StubRetrieval:
        async def retrieve(self, **kwargs: Any) -> list[Any]:
            return []

        async def find_similar(self, **kwargs: Any) -> list[Any]:
            return []

    assert isinstance(_StubRetrieval(), RetrievalPort)


def test_extraction_adapter_shape_satisfies_port() -> None:
    class _StubExtractor:
        async def extract(self, request: Any) -> Any:
            return request

    assert isinstance(_StubExtractor(), ExtractionPort)


def test_retrieval_hit_and_result_shapes() -> None:
    hit = RetrievalHit(
        entity_type=EntityType.REPOSITORY,
        entity_id="42",
        point_id="00000000-0000-0000-0000-000000000000",
        score=0.9,
        distance=0.1,
        payload={"repository_id": 42},
    )
    result = RetrievalResult(hits=[hit], total=1)
    assert result.hits[0].entity_type is EntityType.REPOSITORY
    assert result.hits[0].hydrated is None
    assert result.total == 1
    # StrEnum value equality is the invariant Qdrant payload matching relies on.
    assert EntityType.SUMMARY == "summary"
    assert EntityType.SUMMARY.value == "summary"


def test_graph_dependencies_packs_ports() -> None:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.di.graphs import build_summarize_deps

    sentinel: Any = object()
    deps = build_summarize_deps(
        llm_client=sentinel,
        retrieval=sentinel,
        extraction=sentinel,
        stream_sink=sentinel,
        summaries=sentinel,
        requests=sentinel,
        summary_index=sentinel,
    )
    assert isinstance(deps, SummarizeDeps)
    assert deps.retrieval is sentinel
    assert deps.summary_index is sentinel
    # RAG knobs default to off / 5 unless the composition root overrides them.
    assert deps.rag_enabled is False
    assert deps.rag_top_k == 5
