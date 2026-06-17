"""SummarizeState serialization contract (ADR-0011 / ADR-0004).

The strict-msgpack invariant (no pickle fallback) requires every state field to be
a plain serializable primitive. ``json.dumps`` is a CI-safe proxy (JSON-serializable
=> msgpack-serializable for str/int/list/dict; a port/session/Pydantic leak raises),
and the real langgraph ``JsonPlusSerializer`` round-trip runs locally (skipped where
the optional ``graph`` extra is absent).
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


def test_real_msgpack_roundtrip_with_langgraph_serializer() -> None:
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()
    restored = serde.loads_typed(serde.dumps_typed(_full_state()))
    assert restored == _full_state()
    # The serializer can ENCODE richer types (Pydantic/datetime/dataclass) via
    # msgpack EXT tags without raising, so msgpack-encodability alone does not prove
    # primitiveness. Assert the restored state survives a JSON round-trip => the
    # real guard that a non-primitive did not leak (ADR-0011).
    assert json.loads(json.dumps(restored)) == restored
