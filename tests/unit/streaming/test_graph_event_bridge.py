"""GraphEventBridge translates astream_events into ordered StreamSink calls
(ADR-0017): node transitions -> stages, token deltas -> section snapshots, no
terminal done/error, assembler kept ephemeral."""

from __future__ import annotations

from typing import Any

from app.adapters.content.streaming.graph_event_bridge import (
    SUMMARY_TOKEN_EVENT,
    GraphEventBridge,
)
from app.application.dto.stream_enums import ProcessingStage

_RID = "42"
_CID = "corr-xyz"


class RecordingSink:
    """Records port calls as (method, kwargs) tuples in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def stage(self, *, request_id: str, correlation_id: str, stage: Any) -> None:
        self.calls.append(("stage", {"stage": stage, "rid": request_id, "cid": correlation_id}))

    async def section(
        self,
        *,
        request_id: str,
        correlation_id: str,
        section: str,
        content: str,
        partial: bool = False,
    ) -> None:
        self.calls.append(
            (
                "section",
                {
                    "section": section,
                    "content": content,
                    "partial": partial,
                    "rid": request_id,
                    "cid": correlation_id,
                },
            )
        )

    async def warning(self, **kw: Any) -> None:
        self.calls.append(("warning", kw))

    async def done(self, **kw: Any) -> None:
        self.calls.append(("done", kw))

    async def error(self, **kw: Any) -> None:
        self.calls.append(("error", kw))


def _bridge() -> tuple[GraphEventBridge, RecordingSink]:
    sink = RecordingSink()
    return GraphEventBridge(sink=sink, request_id=_RID, correlation_id=_CID), sink


def _node_start(node: str) -> dict[str, Any]:
    return {"event": "on_chain_start", "name": node, "metadata": {"langgraph_node": node}}


def _token(delta: str) -> dict[str, Any]:
    return {"event": "on_custom_event", "name": SUMMARY_TOKEN_EVENT, "data": delta}


async def _drive(bridge: GraphEventBridge, events: list[dict[str, Any]]) -> None:
    for event in events:
        await bridge.dispatch(event)


async def test_node_transitions_map_to_stages_in_order() -> None:
    bridge, sink = _bridge()
    await _drive(
        bridge,
        [
            _node_start(n)
            for n in ("ingest", "extract", "summarize", "validate", "persist", "notify")
        ],
    )
    stages = [c[1]["stage"] for c in sink.calls if c[0] == "stage"]
    # ingest is unmapped -> no stage; the five canonical stages in spine order.
    assert stages == [
        ProcessingStage.EXTRACTING,
        ProcessingStage.SUMMARIZING,
        ProcessingStage.VALIDATING,
        ProcessingStage.PERSISTING,
        ProcessingStage.DONE,
    ]
    # correlation_id + request_id ride every emitted call.
    assert all(c[1]["cid"] == _CID and c[1]["rid"] == _RID for c in sink.calls if c[0] == "stage")


async def test_duplicate_node_start_emits_stage_once() -> None:
    bridge, sink = _bridge()
    # langgraph fires nested on_chain_start events all tagged with the node.
    await _drive(bridge, [_node_start("summarize"), _node_start("summarize")])
    assert [c[1]["stage"] for c in sink.calls] == [ProcessingStage.SUMMARIZING]


async def test_token_deltas_emit_ordered_section_snapshots() -> None:
    bridge, sink = _bridge()
    # A single complete-JSON delta yields one snapshot per section in _SECTION_ORDER.
    await _drive(bridge, [_token('{"summary_250": "Hello world", "tldr": "Short"}')])
    sections = [
        (c[1]["section"], c[1]["content"], c[1]["partial"]) for c in sink.calls if c[0] == "section"
    ]
    assert sections == [
        ("summary_250", "Hello world", False),
        ("tldr", "Short", False),
    ]


async def test_incremental_string_forwards_each_growing_snapshot() -> None:
    bridge, sink = _bridge()
    # The assembler (shared with the legacy coordinator) emits growing partial
    # snapshots as the string streams in; the bridge forwards each one verbatim.
    await _drive(bridge, [_token('{"summary_250": "Hel'), _token('lo"}')])
    contents = [c[1]["content"] for c in sink.calls if c[0] == "section"]
    assert contents[0] == "Hel"  # eager partial (extract_json repairs the open string)
    assert contents[-1] == "Hello"  # final value
    assert all(c[1]["section"] == "summary_250" for c in sink.calls if c[0] == "section")


async def test_list_section_serialized_like_legacy() -> None:
    bridge, sink = _bridge()
    # Matches SummaryDraftStreamCoordinator: json.dumps(value, ensure_ascii=False).
    await _drive(bridge, [_token('{"topic_tags": ["ai", "ml"]}')])
    section = next(c[1] for c in sink.calls if c[0] == "section")
    assert section["section"] == "topic_tags"
    assert section["content"] == '["ai", "ml"]'


async def test_chat_model_stream_is_a_token_fallback() -> None:
    bridge, sink = _bridge()

    class _Chunk:
        content = '{"tldr": "via chat model"}'

    await bridge.dispatch({"event": "on_chat_model_stream", "data": {"chunk": _Chunk()}})
    section = next(c[1] for c in sink.calls if c[0] == "section")
    assert section == {
        "section": "tldr",
        "content": "via chat model",
        "partial": False,
        "rid": _RID,
        "cid": _CID,
    }


async def test_no_terminal_done_or_error_emitted() -> None:
    bridge, sink = _bridge()
    await _drive(
        bridge,
        [
            _node_start("notify"),
            {"event": "on_chain_end", "name": "LangGraph", "data": {"output": {}}},
            _token('{"summary_250": "x"}'),
        ],
    )
    kinds = {c[0] for c in sink.calls}
    # Terminal done/error stay with BackgroundProgressPublisher (single emitter).
    assert "done" not in kinds and "error" not in kinds and "warning" not in kinds


async def test_assembler_is_per_instance_and_not_shared() -> None:
    b1, _ = _bridge()
    b2, _ = _bridge()
    assert b1._assembler is not b2._assembler  # ephemeral, never checkpoint state


async def test_token_delta_with_non_string_data_is_ignored() -> None:
    bridge, sink = _bridge()
    # A malformed custom event (data not a str) must not feed the assembler or raise.
    await bridge.dispatch(
        {"event": "on_custom_event", "name": SUMMARY_TOKEN_EVENT, "data": {"x": 1}}
    )
    await bridge.dispatch({"event": "on_custom_event", "name": SUMMARY_TOKEN_EVENT, "data": 123})
    assert sink.calls == []


async def test_chat_model_stream_with_empty_content_is_ignored() -> None:
    bridge, sink = _bridge()

    class _Empty:
        content = ""

    await bridge.dispatch({"event": "on_chat_model_stream", "data": {"chunk": _Empty()}})
    assert sink.calls == []
