"""SummarizeState serialization contract (ADR-0011 / ADR-0004).

The strict-msgpack invariant (no pickle fallback) requires every state field to be
a plain serializable primitive. ``json.dumps`` is a CI-safe proxy (JSON-serializable
=> msgpack-serializable for str/int/list/dict; a port/session/Pydantic leak raises),
and the real LangGraph ``JsonPlusSerializer`` round-trip runs in every ordinary
test environment.
"""

from __future__ import annotations

import json

import pytest

from app.application.graphs.summarize.state import MAX_REPAIR_ATTEMPTS, SummarizeState


def _full_state() -> SummarizeState:
    return {
        "correlation_id": "corr-123",
        "request_id": 42,
        "lang": "en",
        "grounding_ids": ["a", "b"],
        "summary": {"tldr": "x", "sections": [{"k": "v"}]},
        "validation_errors": ["too_long"],
        "repair_attempts": 1,
        "call_count": 3,
    }


def test_state_is_json_serializable_primitives_only() -> None:
    blob = json.dumps(_full_state())
    assert json.loads(blob) == _full_state()


def test_every_field_value_is_a_serializable_primitive() -> None:
    allowed = (str, int, list, dict)
    for key, value in _full_state().items():
        assert isinstance(value, allowed), f"{key} is not a serializable primitive"


def test_max_repair_attempts_is_positive() -> None:
    assert MAX_REPAIR_ATTEMPTS >= 1


def test_state_annotations_are_serializable_primitives() -> None:
    # Ties the guard to the real schema (runs without langgraph): a future
    # non-primitive field on SummarizeState fails CI here.
    import types
    import typing

    # bool is JSON/msgpack-serializable (e.g. the ``stream`` mode flag, ADR-0017).
    # ``NoneType`` is serializable too (``request_id`` is ``int | None`` for the
    # content-only path -- no request row, audit #1).
    _PRIMITIVES = {str, int, bool, list, dict, type(None)}

    def _is_primitive(hint: object) -> bool:
        origin = typing.get_origin(hint) or hint
        # Optional / unions of primitives (e.g. ``int | None``) are serializable iff
        # every member is a primitive.
        if origin in {typing.Union, types.UnionType}:
            return all(_is_primitive(arg) for arg in typing.get_args(hint))
        return origin in _PRIMITIVES

    hints = typing.get_type_hints(SummarizeState)
    for name, hint in hints.items():
        assert _is_primitive(hint), f"{name}: {hint!r} is not a serializable primitive"


# ── audit #7: bulk-field drift guard + checkpoint-size ceiling ────────────────

# The transient bulk-text fields (single-run, adjacent-node handoffs documented in
# state.py). Graph assembly must map every one to an UntrackedValue channel.
_UNTRACKED_BULK_FIELDS = frozenset(
    {
        "source_text",
        "requested_system_prompt",
        "feedback_instructions",
        "grounding_block",
        "system_prompt",
        "messages",
        "content_for_summary",
    }
)

# Id-based / small-primitive fields that must NEVER grow into bulk content. (The
# full schema minus the allowlisted bulk fields.)
_EXPECTED_NON_BULK_FIELDS = frozenset(
    {
        "correlation_id",
        "request_id",
        "lang",
        "input_url",
        "stream",
        "two_pass_eligible",
        "dedupe_hash",
        "content_source",
        "detected_lang",
        "title",
        "images",
        "user_scope",
        "environment",
        "user_id",
        "grounding_ids",
        "model_override",
        "max_tokens",
        "summary",
        "summary_id",
        "validation_errors",
        "repair_attempts",
        "call_count",
        "llm_calls",
    }
)


def test_bulk_fields_match_documented_allowlist() -> None:
    """The set of bulk-text fields on SummarizeState must match the reviewed allowlist.

    audit #7 drift guard: ADR-0011 promises a minimal id-based state. The five
    documented bulk handoff fields are the only sanctioned exceptions. Adding a new
    str/list/dict field that is meant to carry bulk content (or removing one of the
    handoffs) must be a conscious change here + in the state.py docstring, not a
    silent enlargement of every Postgres checkpoint.
    """
    import typing

    hints = set(typing.get_type_hints(SummarizeState).keys())
    # Every schema field is accounted for as either bulk or non-bulk (no orphan).
    assert hints == (_UNTRACKED_BULK_FIELDS | _EXPECTED_NON_BULK_FIELDS), (
        "SummarizeState fields drifted from the reviewed bulk/non-bulk partition; "
        "update _UNTRACKED_BULK_FIELDS / _EXPECTED_NON_BULK_FIELDS and the state.py "
        "docstring after confirming the new field's checkpoint-size impact."
    )
    # The bulk allowlist stays a strict subset (no accidental bulk-field growth).
    assert hints >= _UNTRACKED_BULK_FIELDS


def test_compiled_graph_never_checkpoints_bulk_handoffs() -> None:
    from unittest.mock import MagicMock

    from langgraph.channels import UntrackedValue
    from langgraph.checkpoint.memory import InMemorySaver

    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.graph import build_summarize_graph

    port = MagicMock()
    deps = SummarizeDeps(
        llm_client=port,
        retrieval=port,
        extraction=port,
        stream_sink=port,
        summaries=port,
        requests=port,
        summary_index=port,
    )
    graph = build_summarize_graph(deps=deps, checkpointer=InMemorySaver())

    for field in _UNTRACKED_BULK_FIELDS:
        assert isinstance(graph.channels[field], UntrackedValue), field


async def test_checkpoint_snapshot_contains_no_bulk_payload() -> None:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from app.application.graphs.summarize.graph import _checkpoint_state_schema

    async def populate(_state: dict) -> dict:
        return {
            "source_text": "secret source",
            "grounding_block": "secret grounding",
            "requested_system_prompt": "secret requested prompt",
            "feedback_instructions": "secret feedback",
            "system_prompt": "secret system prompt",
            "messages": [{"role": "user", "content": "secret source"}],
            "content_for_summary": "secret source",
        }

    async def stop_before_terminal(_state: dict) -> dict:
        raise RuntimeError("inspect pending checkpoint")

    builder = StateGraph(_checkpoint_state_schema())
    builder.add_node("populate", populate)
    builder.add_node("stop", stop_before_terminal)
    builder.add_edge(START, "populate")
    builder.add_edge("populate", "stop")
    builder.add_edge("stop", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "bulk-check"}}

    with pytest.raises(RuntimeError, match="inspect pending checkpoint"):
        await graph.ainvoke({"correlation_id": "bulk-check"}, config=config)

    snapshot = await graph.aget_state(config)
    assert not (_UNTRACKED_BULK_FIELDS & snapshot.values.keys())


def test_initial_state_carries_no_bulk_prompt_fields() -> None:
    """build_initial_state must not seed the build_prompt-owned bulk fields.

    system_prompt / messages / content_for_summary are written mid-graph by
    build_prompt and consumed by the very next nodes; they must never be part of the
    per-invocation seed (which would bloat the first checkpoint with empty payloads
    and invite callers to pre-populate them out of band).
    """
    from app.application.graphs.summarize.graph import build_initial_state

    state = build_initial_state(correlation_id="c", request_id=1, lang="en")
    for field in ("system_prompt", "messages", "content_for_summary"):
        assert field not in state


def test_checkpoint_size_stays_under_ceiling_for_typical_run() -> None:
    """A representative fully-populated state stays under a sane checkpoint ceiling.

    A coarse regression backstop for audit #7: if a future change starts copying the
    full crawl row / raw HTML / base64 images into state, a typical run's serialized
    checkpoint would blow past this ceiling. The bulk handoff fields here are sized
    to realistic upper bounds (a long article + a full prompt + messages).
    """
    long_text = "word " * 4000  # ~20 KB of cleaned article text
    state: SummarizeState = {
        "correlation_id": "corr-xyz",
        "request_id": 1,
        "lang": "en",
        "input_url": "https://example.com/a",
        "stream": False,
        "dedupe_hash": "deadbeef" * 8,
        "content_source": "scraper",
        "detected_lang": "en",
        "title": "A Reasonably Long Article Title",
        "images": ["https://example.com/img1.png", "https://example.com/img2.png"],
        "user_scope": "owner",
        "environment": "prod",
        "user_id": 7,
        "source_text": long_text,
        "grounding_ids": ["s1", "s2", "s3"],
        "grounding_block": "related prior summaries (reference only)\n" + ("ref " * 200),
        "system_prompt": "You are a summarizer.\n" + ("rule " * 300),
        "messages": [
            {"role": "system", "content": "You are a summarizer.\n" + ("rule " * 300)},
            {"role": "user", "content": long_text},
        ],
        "content_for_summary": long_text,
        "model_override": "some/model",
        "max_tokens": 2048,
        "summary": {"tldr": "x", "summary_250": "y", "sections": [{"k": "v"}]},
        "summary_id": 99,
        "validation_errors": [],
        "repair_attempts": 0,
        "call_count": 1,
        "llm_calls": [{"attempt_index": 1, "model": "m"}],
    }
    size = len(json.dumps(state).encode("utf-8"))
    # ~256 KB ceiling: comfortably above a long-article run, far below what raw
    # HTML / crawl-row / base64-image duplication into state would produce.
    assert size < 256 * 1024, f"typical checkpoint grew to {size} bytes"


def test_real_msgpack_roundtrip_with_langgraph_serializer() -> None:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()
    restored = serde.loads_typed(serde.dumps_typed(_full_state()))
    assert restored == _full_state()
    # The serializer can ENCODE richer types (Pydantic/datetime/dataclass) via
    # msgpack EXT tags without raising, so msgpack-encodability alone does not prove
    # primitiveness. Assert the restored state survives a JSON round-trip => the
    # real guard that a non-primitive did not leak (ADR-0011).
    assert json.loads(json.dumps(restored)) == restored
