"""Summarize node stubs: per-node OTel span + serializable, deps-free updates.

CI-safe (no langgraph): nodes import only the OTel helpers + ports, never langgraph.
"""

from __future__ import annotations

import importlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.application.graphs.summarize.nodes._span as span_mod
from app.application.graphs.summarize import nodes
from app.application.graphs.summarize.lifecycle import CallBudgetExceeded
from app.application.graphs.summarize.state import MAX_REPAIR_ATTEMPTS, SummarizeState
from app.observability.attributes import GRAPH_NODE, GRAPH_THREAD_ID, REQUEST_CORRELATION_ID

ALL_NODES = [
    nodes.ingest,
    nodes.extract,
    nodes.ground,
    nodes.build_prompt,
    nodes.summarize,
    nodes.validate,
    nodes.repair,
    nodes.enrich,
    nodes.persist,
    nodes.notify,
]


def _state(**over: object) -> SummarizeState:
    base: dict = {
        "correlation_id": "cid-xyz",
        "request_id": 7,
        "lang": "en",
        "grounding_ids": [],
        "summary": {},
        "validation_errors": [],
        "repair_attempts": 0,
        "call_count": 0,
    }
    base.update(over)
    return base  # type: ignore[return-value]


@pytest.mark.parametrize("node", ALL_NODES, ids=lambda n: n.__name__)
async def test_node_returns_serializable_update_without_deps(node) -> None:
    deps = MagicMock(name="deps")
    result = await node(_state(), deps=deps)
    assert isinstance(result, dict)
    # Live deps must never leak into the (checkpointed) state update (ADR-0011).
    assert deps not in result.values()
    # Serializable AND round-trip-identical: catches a non-primitive leak (a nested
    # MagicMock raises) and silent drift (tuple->list, int-key->str-key).
    assert json.loads(json.dumps(result)) == result


@pytest.mark.parametrize("node", ALL_NODES, ids=lambda n: n.__name__)
async def test_node_opens_span_carrying_correlation_id(node, monkeypatch) -> None:
    tracer = MagicMock()
    span_cm = MagicMock()
    span_cm.__enter__ = MagicMock(return_value=span_cm)
    span_cm.__exit__ = MagicMock(return_value=False)
    tracer.start_as_current_span = MagicMock(return_value=span_cm)
    set_cid = MagicMock()
    monkeypatch.setattr(span_mod, "_tracer", tracer)
    monkeypatch.setattr(span_mod, "set_correlation_id_attr", set_cid)

    await node(_state(), deps=MagicMock())

    tracer.start_as_current_span.assert_called_once()
    attributes = tracer.start_as_current_span.call_args.kwargs["attributes"]
    assert attributes[REQUEST_CORRELATION_ID] == "cid-xyz"
    assert attributes[GRAPH_THREAD_ID] == "cid-xyz"  # thread_id == correlation_id (sacred)
    assert attributes[GRAPH_NODE] == node.__name__
    set_cid.assert_called_once_with("cid-xyz")


async def test_ground_returns_empty_grounding() -> None:
    out = await nodes.ground(_state(), deps=MagicMock())
    assert out["grounding_ids"] == []


async def test_validate_reports_valid_by_default() -> None:
    out = await nodes.validate(_state(), deps=MagicMock())
    assert out["validation_errors"] == []


async def test_repair_increments_attempts_under_budget() -> None:
    out = await nodes.repair(_state(repair_attempts=0), deps=MagicMock())
    assert out["repair_attempts"] == 1


async def test_repair_raises_call_budget_exceeded_over_budget() -> None:
    with pytest.raises(CallBudgetExceeded):
        await nodes.repair(_state(repair_attempts=MAX_REPAIR_ATTEMPTS), deps=MagicMock())


async def test_repair_prompt_includes_validation_errors_and_prior_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repair node must feed validation_errors + the prior bad candidate back
    to the LLM instead of blindly re-sending the original prompt (see
    ``_build_repair_messages``)."""
    # ``nodes/__init__.py`` shadows the ``nodes.repair`` package attribute with
    # the ``repair`` function (``from .nodes.repair import repair``), so the
    # submodule must be fetched via sys.modules (importlib), not attribute
    # access, to patch its module-local ``summarize_with_instructor`` name.
    repair_mod = importlib.import_module("app.application.graphs.summarize.nodes.repair")
    captured: dict[str, object] = {}

    async def _fake_summarize_with_instructor(*, messages, **kwargs):
        captured["messages"] = messages
        return {"summary_250": "fixed"}, {"model": "m", "tokens_prompt": 1}, 1

    monkeypatch.setattr(
        repair_mod,
        "summarize_with_instructor",
        AsyncMock(side_effect=_fake_summarize_with_instructor),
    )

    original_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ]
    state = _state(
        repair_attempts=0,
        messages=original_messages,
        summary={"summary_250": "bad"},
        validation_errors=["summary_250 too short", "missing tldr"],
    )

    out = await nodes.repair(state, deps=MagicMock())

    assert out["summary"] == {"summary_250": "fixed"}
    repair_messages = captured["messages"]
    # Original messages are preserved, untouched, at the front.
    assert repair_messages[:2] == original_messages
    # An assistant turn carries the prior invalid candidate.
    assert repair_messages[2]["role"] == "assistant"
    assert "bad" in repair_messages[2]["content"]
    # A trailing user turn enumerates exactly the validation errors.
    correction = repair_messages[3]["content"]
    assert repair_messages[3]["role"] == "user"
    assert "summary_250 too short" in correction
    assert "missing tldr" in correction
