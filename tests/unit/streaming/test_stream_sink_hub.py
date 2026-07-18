"""StreamHubStreamSink emits StreamEvents structurally identical to the legacy
producer for every kind (ADR-0017 consumer-parity gate)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.adapters.content.streaming.events import StreamEvent
from app.adapters.content.streaming.stream_hub import StreamHub
from app.adapters.content.streaming.stream_sink_hub import StreamHubStreamSink
from app.application.dto.stream_enums import ProcessingStage
from app.application.ports.stream_sink import StreamSinkPort

_CID = "corr-123"
_RID = "42"


class RecordingHub(StreamHub):
    """Captures published events, mirroring tests/integration RecordingHub."""

    def __init__(self) -> None:
        self.events: list[tuple[str, StreamEvent]] = []

    def publish(self, request_id: str, event: StreamEvent) -> None:
        self.events.append((request_id, event))


def _sink() -> tuple[StreamHubStreamSink, RecordingHub]:
    hub = RecordingHub()
    return StreamHubStreamSink(hub=hub), hub


def test_adapter_is_structural_stream_sink_port() -> None:
    sink, _ = _sink()
    assert isinstance(sink, StreamSinkPort)


async def test_stage_event_shape_matches_legacy() -> None:
    sink, hub = _sink()
    await sink.stage(request_id=_RID, correlation_id=_CID, stage=ProcessingStage.SUMMARIZING)

    rid, event = hub.events[0]
    assert rid == _RID
    assert event.kind == "stage"
    # StrEnum value equals the legacy plain-string payload byte-for-byte.
    assert event.payload == {"stage": "summarizing"}
    assert event.correlation_id == _CID


async def test_stage_accepts_raw_string() -> None:
    sink, hub = _sink()
    await sink.stage(request_id=_RID, correlation_id=_CID, stage="validating")
    assert hub.events[0][1].payload == {"stage": "validating"}


async def test_section_event_shape_matches_legacy() -> None:
    sink, hub = _sink()
    await sink.section(
        request_id=_RID, correlation_id=_CID, section="summary_250", content="Hello world"
    )
    event = hub.events[0][1]
    assert event.kind == "section"
    # Legacy SummaryDraftStreamCoordinator publishes partial=False always.
    assert event.payload == {"section": "summary_250", "content": "Hello world", "partial": False}
    assert event.correlation_id == _CID


async def test_section_event_is_persisted_for_cross_process_sse() -> None:
    hub = RecordingHub()
    repo = AsyncMock()
    sink = StreamHubStreamSink(hub=hub, progress_event_repo=repo)

    await sink.section(
        request_id=_RID,
        correlation_id=_CID,
        section="summary_250",
        content="Durable preview",
        partial=True,
    )

    repo.append.assert_awaited_once_with(
        request_id=42,
        kind="section",
        stage=None,
        status="processing",
        message="Durable preview",
        progress=None,
        payload={"section": "summary_250", "content": "Durable preview", "partial": True},
        correlation_id=_CID,
    )
    assert hub.events[0][1].kind == "section"


async def test_warning_event_shape() -> None:
    sink, hub = _sink()
    await sink.warning(request_id=_RID, correlation_id=_CID, code="LOW_VALUE", message="thin")
    event = hub.events[0][1]
    assert event.kind == "warning"
    assert event.payload == {"code": "LOW_VALUE", "message": "thin", "correlation_id": _CID}


async def test_done_event_shape() -> None:
    sink, hub = _sink()
    await sink.done(request_id=_RID, correlation_id=_CID, summary_id="9")
    event = hub.events[0][1]
    assert event.kind == "done"
    assert event.payload == {"summary_id": "9", "request_id": _RID}


async def test_done_event_allows_null_summary_id() -> None:
    sink, hub = _sink()
    await sink.done(request_id=_RID, correlation_id=_CID, summary_id=None)
    assert hub.events[0][1].payload == {"summary_id": None, "request_id": _RID}


async def test_error_event_shape() -> None:
    sink, hub = _sink()
    await sink.error(request_id=_RID, correlation_id=_CID, code="FAILED", message="boom")
    event = hub.events[0][1]
    assert event.kind == "error"
    assert event.payload == {"code": "FAILED", "message": "boom", "correlation_id": _CID}


async def test_falls_back_to_process_global_hub(monkeypatch) -> None:
    """With no injected hub, the adapter resolves get_stream_hub() lazily."""
    import app.adapters.content.streaming.stream_sink_hub as mod

    hub = RecordingHub()
    monkeypatch.setattr(mod, "get_stream_hub", lambda: hub)
    await StreamHubStreamSink().stage(
        request_id=_RID, correlation_id=_CID, stage=ProcessingStage.EXTRACTING
    )
    assert hub.events[0][1].payload == {"stage": "extracting"}
