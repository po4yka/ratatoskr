"""Bridge LangGraph ``astream_events`` into the ``StreamSinkPort`` (ADR-0017).

The summarize graph is the producer; this bridge is the single translation
surface from langgraph's event stream to the in-process ``StreamHub`` (via the
injected sink). It imports **no langgraph type** -- it consumes plain event
dicts (the astream_events v2 schema), so it stays outside the LangGraph import
budget and is unit-testable with hand-built event sequences.

Token-feed decision (ADR-0017 work item 4)
------------------------------------------
ADR-0017 mandates ``astream_events`` as the producer. We drive **section**
extraction off the model *content* stream the assembler already expects -- not
LangChain message-wrapper objects. The summarize node (T7) emits each summary
token delta as a custom event named ``summary_token`` whose ``data`` is the raw
delta string; the bridge feeds that straight into
:class:`SummarySectionStreamAssembler`. As a defensive fallback the bridge also
reads ``on_chat_model_stream`` content, in case T7 wraps a streaming chat model.

Stage events come from **node transitions**: the first ``on_chain_start`` for a
mapped node emits its ``ProcessingStage`` (deduped, since langgraph fires nested
start events that all carry the same ``langgraph_node``).

Terminal ``done`` / ``error`` are deliberately NOT emitted by the bridge: the
streaming runner owns that lifecycle and emits one terminal event after graph
success or durable failure routing. The assembler is per-instance and ephemeral
-- it never enters checkpoint state (ADR-0011).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.adapters.content.streaming.section_assembler import SummarySectionStreamAssembler
from app.application.dto.stream_enums import SUMMARY_TOKEN_EVENT, ProcessingStage

if TYPE_CHECKING:
    from app.application.ports.stream_sink import StreamSinkPort

# Re-exported for callers that import the event name from the bridge; the single
# source of truth is app.application.dto.stream_enums (shared with the producer node).
__all__ = ["SUMMARY_TOKEN_EVENT", "GraphEventBridge"]

# node name -> the stage emitted when that node starts. Nodes not listed
# (ingest/ground/build_prompt/repair/enrich) emit no stage, matching the five
# legacy stages (extracting/summarizing/validating/persisting/done).
_NODE_STAGE: dict[str, ProcessingStage] = {
    "extract": ProcessingStage.EXTRACTING,
    "summarize": ProcessingStage.SUMMARIZING,
    "validate": ProcessingStage.VALIDATING,
    "persist": ProcessingStage.PERSISTING,
    "notify": ProcessingStage.DONE,
}


class GraphEventBridge:
    """Per-invocation translator: ``astream_events`` dict -> StreamSink calls."""

    def __init__(self, *, sink: StreamSinkPort, request_id: str, correlation_id: str) -> None:
        self._sink = sink
        self._request_id = request_id
        self._correlation_id = correlation_id
        # Ephemeral, per-invocation: NEVER serialized into SummarizeState (ADR-0011).
        self._assembler = SummarySectionStreamAssembler()
        self._emitted_stages: set[str] = set()

    async def dispatch(self, event: dict[str, Any]) -> None:
        """Translate one astream_events event into 0..n sink calls."""
        if event.get("event") == "on_chain_start":
            await self._maybe_emit_stage(event)
            return
        delta = _extract_token_delta(event)
        if delta:
            await self._emit_sections(delta)

    async def _maybe_emit_stage(self, event: dict[str, Any]) -> None:
        node = (event.get("metadata") or {}).get("langgraph_node") or event.get("name")
        stage = _NODE_STAGE.get(node or "")
        if stage is None or stage.value in self._emitted_stages:
            return
        self._emitted_stages.add(stage.value)
        await self._sink.stage(
            request_id=self._request_id, correlation_id=self._correlation_id, stage=stage
        )

    async def _emit_sections(self, delta: str) -> None:
        for snap in self._assembler.add_delta(delta):
            # Match the legacy SummaryDraftStreamCoordinator exactly: list values
            # serialize via json.dumps(ensure_ascii=False); partial is always False.
            content = (
                json.dumps(snap.value, ensure_ascii=False)
                if isinstance(snap.value, list)
                else snap.value
            )
            await self._sink.section(
                request_id=self._request_id,
                correlation_id=self._correlation_id,
                section=snap.section,
                content=content,
                partial=False,
            )


def _extract_token_delta(event: dict[str, Any]) -> str | None:
    """Pull a summary token delta out of an astream_events event, else ``None``."""
    kind = event.get("event")
    if kind == "on_custom_event" and event.get("name") == SUMMARY_TOKEN_EVENT:
        data = event.get("data")
        return data if isinstance(data, str) else None
    if kind == "on_chat_model_stream":
        chunk = event.get("data", {}).get("chunk")
        content = getattr(chunk, "content", None)
        return content if isinstance(content, str) and content else None
    return None
