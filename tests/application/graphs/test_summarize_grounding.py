"""T6: ground / build_prompt / persist grounding + read-your-writes behavior.

CI-safe (no langgraph, no Qdrant, no Postgres): nodes are plain
``async def(state, *, deps)`` exercised with fake ports.
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
    rag_enabled: bool = False,
    rag_top_k: int = 5,
) -> SummarizeDeps:
    return SummarizeDeps(
        llm_client=MagicMock(),
        retrieval=retrieval or _FakeRetrieval(),
        extraction=MagicMock(),
        stream_sink=MagicMock(),
        summaries=MagicMock(),
        requests=MagicMock(),
        summary_index=summary_index or _FakeSummaryIndex(),
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
# ground node
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
# build_prompt node (grounding concatenation seam)
# --------------------------------------------------------------------------- #


async def test_build_prompt_noop_without_grounding_block() -> None:
    # Flag-off parity: no grounding block -> no prompt delta.
    assert await build_prompt(_grounded_state(grounding_block=""), deps=_deps()) == {}


async def test_build_prompt_appends_block_to_base_system_prompt() -> None:
    state = _grounded_state(grounding_block="=== BLOCK ===\n1. x", system_prompt="BASE PROMPT")
    out = await build_prompt(state, deps=_deps())
    assert out == {"system_prompt": "BASE PROMPT\n\n=== BLOCK ===\n1. x"}


async def test_build_prompt_uses_block_alone_when_no_base() -> None:
    state = _grounded_state(grounding_block="=== BLOCK ===")
    out = await build_prompt(state, deps=_deps())
    assert out == {"system_prompt": "=== BLOCK ==="}


# --------------------------------------------------------------------------- #
# persist node (read-your-writes fast-path)
# --------------------------------------------------------------------------- #


async def test_persist_indexes_summary_on_write() -> None:
    index = _FakeSummaryIndex()
    state = _grounded_state(summary={"tldr": "hi"}, summary_id=100)
    out = await persist(state, deps=_deps(summary_index=index))
    assert out == {}
    assert len(index.calls) == 1
    call = index.calls[0]
    assert call["request_id"] == 42
    assert call["summary_id"] == 100
    assert call["summary"] == {"tldr": "hi"}
    assert call["lang"] == "en"
    assert call["scope"].user_scope == "public"
    assert call["correlation_id"] == "cid-1"


async def test_persist_skips_when_no_summary_id() -> None:
    index = _FakeSummaryIndex()
    state = _grounded_state(summary={"tldr": "hi"})  # no summary_id yet
    await persist(state, deps=_deps(summary_index=index))
    assert index.calls == []


async def test_persist_swallows_index_failure_and_completes() -> None:
    # Resilience: a Qdrant failure must NOT propagate (ADR-0012).
    index = _FakeSummaryIndex(raises=True)
    state = _grounded_state(summary={"tldr": "hi"}, summary_id=100)
    out = await persist(state, deps=_deps(summary_index=index))
    assert out == {}  # request completion is never blocked
    assert len(index.calls) == 1  # it was attempted


@pytest.mark.parametrize("enabled", [True, False])
async def test_ground_then_build_prompt_flag_parity(enabled: bool) -> None:
    """End-to-end node parity: flag off => build_prompt produces no delta."""
    fake = _FakeRetrieval(hits=[_summary_hit("1", title="Prior", tldr="t")])
    deps = _deps(retrieval=fake, rag_enabled=enabled)
    state = _grounded_state()
    state.update(await ground(state, deps=deps))
    out = await build_prompt(state, deps=deps)
    if enabled:
        assert out["system_prompt"].startswith(GROUNDING_BLOCK_HEADER)
    else:
        assert out == {}  # byte-identical to the no-RAG path
