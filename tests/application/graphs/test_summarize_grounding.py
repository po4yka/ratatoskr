"""T6/T7: ground / build_prompt / persist grounding + read-your-writes behavior.

CI-safe (no langgraph, no Qdrant, no Postgres): nodes are plain
``async def(state, *, deps)`` exercised with fake ports. The ground-node tests are
T6; the build_prompt + persist tests track the T7 node bodies (full prompt
assembly; summary + llm_calls persistence + freshness index).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.application.dto.vector_search import (
    EntityType,
    RetrievalHit,
    RetrievalResult,
    RetrievalScope,
)
from app.application.graphs.summarize.deps import SummarizeDeps
from app.application.graphs.summarize.nodes import build_prompt, ground, persist
from app.application.graphs.summarize.nodes.ground import (
    GROUNDING_BLOCK_FOOTER,
    GROUNDING_BLOCK_HEADER,
)


class _FakeRetrieval:
    """Records the retrieve() call and returns a canned result."""

    def __init__(self, hits: list[RetrievalHit] | None = None) -> None:
        self._hits = hits or []
        self.calls: list[dict[str, Any]] = []

    async def retrieve(self, **kwargs: Any) -> RetrievalResult:
        self.calls.append(kwargs)
        return RetrievalResult(hits=self._hits, total=len(self._hits))

    async def find_similar(self, **kwargs: Any) -> RetrievalResult:  # pragma: no cover - unused
        return RetrievalResult(hits=[], total=0)


class _FakeSummaryIndex:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def index_summary(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.raises:
            raise RuntimeError("qdrant down")


class _FakeSummaries:
    """Records finalize calls and returns a canned summary id."""

    def __init__(self, *, summary_id: int | None = None) -> None:
        self._summary_id = summary_id
        self.finalized: list[dict[str, Any]] = []

    async def async_finalize_request_summary(self, **kwargs: Any) -> int:
        self.finalized.append(kwargs)
        return 1

    async def async_get_summary_id_by_request(self, request_id: int) -> int | None:
        return self._summary_id


class _FakeLLMRepo:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def async_insert_llm_call(self, record: dict[str, Any]) -> int:
        self.records.append(dict(record))
        return len(self.records)


def _summary_hit(
    entity_id: str, *, title: str, tldr: str = "", summary_250: str = ""
) -> RetrievalHit:
    payload: dict[str, Any] = {"summary_id": int(entity_id), "title": title}
    if tldr:
        payload["tldr"] = tldr
    if summary_250:
        payload["summary_250"] = summary_250
    return RetrievalHit(
        entity_type=EntityType.SUMMARY,
        entity_id=entity_id,
        point_id=f"pid-{entity_id}",
        score=0.9,
        distance=0.1,
        payload=payload,
    )


def _deps(
    *,
    retrieval: Any = None,
    summary_index: Any = None,
    summaries: Any = None,
    llm_repo: Any = None,
    rag_enabled: bool = False,
    rag_top_k: int = 5,
) -> SummarizeDeps:
    # _FakeSummaries implements only the methods the persist node calls; typed Any
    # so mypy accepts it as the (much wider) SummaryRepositoryPort.
    summaries_port: Any = summaries or _FakeSummaries()
    return SummarizeDeps(
        llm_client=MagicMock(),
        retrieval=retrieval or _FakeRetrieval(),
        extraction=MagicMock(),
        stream_sink=MagicMock(),
        summaries=summaries_port,
        requests=MagicMock(),
        summary_index=summary_index or _FakeSummaryIndex(),
        llm_repo=llm_repo,
        rag_enabled=rag_enabled,
        rag_top_k=rag_top_k,
    )


def _grounded_state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "correlation_id": "cid-1",
        "request_id": 42,
        "lang": "en",
        "source_text": "an article about distributed databases",
        "user_scope": "public",
        "environment": "prod",
        "user_id": 7,
        "grounding_ids": [],
        "grounding_block": "",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# ground node (T6)
# --------------------------------------------------------------------------- #


async def test_ground_flag_off_is_noop_and_never_retrieves() -> None:
    fake = _FakeRetrieval(hits=[_summary_hit("1", title="X")])
    out = await ground(_grounded_state(), deps=_deps(retrieval=fake, rag_enabled=False))
    assert out == {"grounding_ids": [], "grounding_block": ""}
    assert fake.calls == []  # flag off: no embedding, no query (ADR-0018 parity)


async def test_ground_noop_when_source_text_missing() -> None:
    fake = _FakeRetrieval(hits=[_summary_hit("1", title="X")])
    out = await ground(
        _grounded_state(source_text=""), deps=_deps(retrieval=fake, rag_enabled=True)
    )
    assert out == {"grounding_ids": [], "grounding_block": ""}
    assert fake.calls == []  # never issue an unscoped/empty query


async def test_ground_noop_when_scope_incomplete() -> None:
    fake = _FakeRetrieval(hits=[_summary_hit("1", title="X")])
    out = await ground(_grounded_state(user_scope=""), deps=_deps(retrieval=fake, rag_enabled=True))
    assert out == {"grounding_ids": [], "grounding_block": ""}
    assert fake.calls == []


async def test_ground_retrieves_with_scope_topk_and_excludes_current_request() -> None:
    fake = _FakeRetrieval(
        hits=[
            _summary_hit("11", title="Raft consensus", tldr="A consensus protocol."),
            _summary_hit("12", title="Paxos", summary_250="The original consensus algorithm."),
        ]
    )
    out = await ground(_grounded_state(), deps=_deps(retrieval=fake, rag_enabled=True, rag_top_k=3))

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["entity_type"] is EntityType.SUMMARY
    assert call["query"] == "an article about distributed databases"
    assert call["top_k"] == 3
    assert call["exclude_request_id"] == 42  # current request excluded
    scope = call["scope"]
    assert isinstance(scope, RetrievalScope)
    # Owner-wide: user_id is NOT applied (summary points carry none); the
    # partition is environment + user_scope.
    assert (scope.environment, scope.user_scope, scope.user_id) == ("prod", "public", None)

    assert out["grounding_ids"] == ["11", "12"]
    block = out["grounding_block"]
    assert block.startswith(GROUNDING_BLOCK_HEADER)
    assert block.endswith(GROUNDING_BLOCK_FOOTER)
    assert "do NOT summarize" in block
    assert "Raft consensus" in block and "A consensus protocol." in block
    assert "Paxos" in block and "The original consensus algorithm." in block


async def test_ground_scope_is_always_owner_wide() -> None:
    # Even when the request carries a user_id, the summary scope stays owner-wide
    # (user_id=None) -- summary Qdrant points have no user_id to filter on.
    fake = _FakeRetrieval(hits=[_summary_hit("9", title="Y")])
    await ground(_grounded_state(user_id=123), deps=_deps(retrieval=fake, rag_enabled=True))
    assert fake.calls[0]["scope"].user_id is None


async def test_ground_empty_when_no_hits() -> None:
    out = await ground(
        _grounded_state(), deps=_deps(retrieval=_FakeRetrieval(hits=[]), rag_enabled=True)
    )
    assert out == {"grounding_ids": [], "grounding_block": ""}


# --------------------------------------------------------------------------- #
# build_prompt node (T7 full assembly + grounding concatenation seam)
# --------------------------------------------------------------------------- #


async def test_build_prompt_assembles_instructor_prompt_without_grounding() -> None:
    # No grounding block -> the system prompt is the instructor prompt (no header),
    # and the LLM messages are assembled (flag-off RAG parity).
    out = await build_prompt(_grounded_state(grounding_block=""), deps=_deps())
    assert GROUNDING_BLOCK_HEADER not in out["system_prompt"]
    assert out["messages"][0]["role"] == "system"
    assert out["messages"][1]["role"] == "user"
    assert "distributed databases" in out["messages"][1]["content"]
    assert out["max_tokens"] >= 1536


async def test_build_prompt_appends_grounding_block_after_instructor_prompt() -> None:
    block = f"{GROUNDING_BLOCK_HEADER}\n1. x\n{GROUNDING_BLOCK_FOOTER}"
    out = await build_prompt(_grounded_state(grounding_block=block), deps=_deps())
    # The block is concatenated after the instructor system prompt (seam preserved).
    assert out["system_prompt"].endswith(block)
    assert GROUNDING_BLOCK_HEADER in out["system_prompt"]
    # And the same system prompt is the first message.
    assert out["messages"][0]["content"] == out["system_prompt"]


async def test_build_prompt_noop_when_no_content_and_no_block() -> None:
    # No extracted content and no grounding -> preserve the T6 grounding-only seam
    # (no prompt delta, byte-identical to the no-RAG no-content path).
    assert (
        await build_prompt(_grounded_state(source_text="", grounding_block=""), deps=_deps()) == {}
    )


@pytest.mark.parametrize("enabled", [True, False])
async def test_ground_then_build_prompt_flag_parity(enabled: bool) -> None:
    """End-to-end node parity: flag off => no grounding block in the prompt."""
    fake = _FakeRetrieval(hits=[_summary_hit("1", title="Prior", tldr="t")])
    deps = _deps(retrieval=fake, rag_enabled=enabled)
    state = _grounded_state()
    state.update(await ground(state, deps=deps))
    out = await build_prompt(state, deps=deps)
    if enabled:
        assert GROUNDING_BLOCK_HEADER in out["system_prompt"]
    else:
        # No grounding leaked into the assembled prompt.
        assert GROUNDING_BLOCK_HEADER not in out["system_prompt"]


# --------------------------------------------------------------------------- #
# persist node (T7: summary + llm_calls + T6 read-your-writes fast-path)
# --------------------------------------------------------------------------- #


async def test_persist_finalizes_summary_and_indexes_on_write() -> None:
    index = _FakeSummaryIndex()
    summaries = _FakeSummaries(summary_id=100)
    llm_repo = _FakeLLMRepo()
    state = _grounded_state(
        summary={"tldr": "hi"},
        llm_calls=[{"request_id": 42, "provider": "openrouter", "attempt_trigger": "graph_node"}],
    )
    out = await persist(
        state, deps=_deps(summary_index=index, summaries=summaries, llm_repo=llm_repo)
    )

    assert out == {"summary_id": 100}
    # Summary finalized (request -> COMPLETED).
    assert len(summaries.finalized) == 1
    assert summaries.finalized[0]["request_id"] == 42
    # llm_calls persisted with the graph_node trigger (persist-everything).
    assert len(llm_repo.records) == 1
    assert llm_repo.records[0]["attempt_trigger"] == "graph_node"
    # Read-your-writes index fired with the resolved summary id.
    assert len(index.calls) == 1
    call = index.calls[0]
    assert call["request_id"] == 42
    assert call["summary_id"] == 100
    assert call["summary"] == {"tldr": "hi"}
    assert call["lang"] == "en"
    assert call["scope"].user_scope == "public"
    assert call["correlation_id"] == "cid-1"


async def test_persist_skips_index_when_no_summary_id() -> None:
    index = _FakeSummaryIndex()
    summaries = _FakeSummaries(summary_id=None)  # id never resolves
    state = _grounded_state(summary={"tldr": "hi"})
    out = await persist(state, deps=_deps(summary_index=index, summaries=summaries))
    # Summary still finalized, but no id -> no freshness index.
    assert len(summaries.finalized) == 1
    assert index.calls == []
    assert out == {}


async def test_persist_swallows_index_failure_and_completes() -> None:
    # Resilience: a Qdrant failure must NOT propagate (ADR-0012).
    index = _FakeSummaryIndex(raises=True)
    summaries = _FakeSummaries(summary_id=100)
    state = _grounded_state(summary={"tldr": "hi"})
    out = await persist(state, deps=_deps(summary_index=index, summaries=summaries))
    assert out == {"summary_id": 100}  # request completion is never blocked
    assert len(index.calls) == 1  # it was attempted


async def test_persist_noop_without_summary() -> None:
    # Nothing produced -> no persistence, no index.
    index = _FakeSummaryIndex()
    summaries = _FakeSummaries(summary_id=100)
    out = await persist(_grounded_state(), deps=_deps(summary_index=index, summaries=summaries))
    assert out == {}
    assert summaries.finalized == []
    assert index.calls == []
