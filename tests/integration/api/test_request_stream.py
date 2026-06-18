"""Integration tests for the /v1/requests/{request_id}/stream SSE endpoint.

These tests patch FastAPI's dependency injection to bypass the DB so they run
without TEST_DATABASE_URL.  The ownership check (`get_request_by_id`) is
replaced by a stub that controls who can access which request.

NOTE: SSE streams stay alive until a terminal event is received or the client
disconnects.  Tests that need to read events must drain until a terminal event
appears and then break rather than iterating the full response body.
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.application.dto.stream_enums import ProcessingStage

pytest.importorskip("jwt", reason="PyJWT not installed")
pytest.importorskip("fastapi", reason="FastAPI not installed")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# User IDs that conftest.py puts in ALLOWED_USER_IDS.
_OWNER_ID = 123456789
_OTHER_ID = 987654321


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app_and_client(
    monkeypatch: pytest.MonkeyPatch,
    owner_id: int = _OWNER_ID,
    progress_repo: Any | None = None,
) -> Any:
    """Build a TestClient with the DB dependency stubbed out.

    The streams router's `_get_request_service` dependency is overridden to
    return a mock RequestService whose `get_request_by_id` only succeeds for
    *owner_id*.

    The SSE heartbeat interval is patched to 0 so that `EventSourceResponse`
    does not block the test thread waiting for a ping tick.
    """
    monkeypatch.setenv("REDIS_ENABLED", "0")
    # Replace the SSE heartbeat with one that sleeps forever (until cancelled).
    # The production _ping() sends comment lines every HEARTBEAT_INTERVAL=15s
    # via anyio.sleep().  sse-starlette runs ping in a cancel_on_finish task:
    # when the event generator task finishes and cancels the task group,
    # _ping is cancelled too.  An infinite sleep achieves the same without
    # blocking the test for 15s waiting for the first ping tick.
    import anyio
    from sse_starlette.sse import AppStatus, EventSourceResponse as _ESR

    async def _wait_forever(self, send):
        await anyio.sleep_forever()

    monkeypatch.setattr(_ESR, "_ping", _wait_forever)

    # Each TestClient call spins up a fresh anyio loop; reset
    # AppStatus.should_exit so sse-starlette doesn't carry over the "should
    # exit" signal from a previous test. (sse-starlette 3.x dropped the
    # explicit should_exit_event attribute and recreates the Event lazily.)
    monkeypatch.setattr(AppStatus, "should_exit", False)

    try:
        from fastapi.testclient import TestClient
    except ImportError:
        from starlette.testclient import TestClient

    import app.api.main as _main_mod

    importlib.reload(_main_mod)
    fastapi_app = _main_mod.app

    # Build a mock RequestService with ownership semantics.
    mock_svc = MagicMock()

    from app.domain.exceptions.domain_exceptions import ResourceNotFoundError as RNFE

    async def _check_ownership(uid: int, rid: int) -> dict:
        if uid != owner_id:
            raise RNFE(f"request {rid} not found for user {uid}")
        return {"id": rid, "user_id": uid}

    mock_svc.get_request_by_id = _check_ownership

    import app.api.routers.content.streams as _streams_mod

    fastapi_app.dependency_overrides[_streams_mod._get_request_service] = lambda: mock_svc
    if progress_repo is not None:
        fastapi_app.dependency_overrides[_streams_mod._get_progress_event_repository] = lambda: (
            progress_repo
        )

    try:
        from app.api import middleware as _mw

        _mw._local_rate_limits.clear()
    except Exception:
        pass

    return TestClient(fastapi_app)


def _patch_hub(monkeypatch: pytest.MonkeyPatch, hub: Any) -> None:
    """Patch get_stream_hub() in both the hub module and the streams router."""
    import app.api.routers.content.streams as _streams_mod
    from app.adapters.content.streaming import stream_hub as _hub_mod

    monkeypatch.setattr(_hub_mod, "get_stream_hub", lambda: hub)
    monkeypatch.setattr(_streams_mod, "get_stream_hub", lambda: hub)


def _make_hub(*events, request_id: str) -> Any:
    """Build a StreamHub pre-loaded with *events* for *request_id*.

    Events are injected directly into the ring buffer rather than via
    ``publish()`` to avoid the synchronous ``_cleanup_request`` path that
    fires when there is no running event loop (which would immediately wipe
    the buffer for terminal events such as ``done``).
    """
    from collections import deque

    from app.adapters.content.streaming.stream_hub import _RING_BUFFER_MAXLEN, StreamHub

    hub = StreamHub()
    if events:
        buf: deque = deque(maxlen=_RING_BUFFER_MAXLEN)
        for ev in events:
            buf.append(ev)
        hub._buffers[request_id] = buf
    return hub


def _parse_event_types(raw_lines: list[str]) -> list[str]:
    """Extract event type names from raw SSE lines."""
    return [
        line.removeprefix("event: ").strip() for line in raw_lines if line.startswith("event: ")
    ]


class _ProgressEvent:
    def __init__(
        self,
        *,
        event_id: str,
        request_id: int,
        sequence: int,
        kind: str,
        stage: str,
        status: str,
    ) -> None:
        self.event_id = event_id
        self.request_id = request_id
        self.sequence = sequence
        self.kind = kind
        self.stage = stage
        self.status = status
        self.message = f"{kind}-{sequence}"
        self.progress = float(sequence) / 10
        self.payload = {"sequence": sequence}
        self.correlation_id = "cid"
        self.created_at = "2026-05-21T00:00:00Z"

    def as_sse_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "request_id": self.request_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "stage": self.stage,
            "status": self.status,
            "message": self.message,
            "progress": self.progress,
            "payload": self.payload,
            "created_at": self.created_at,
            "correlation_id": self.correlation_id,
        }


class _ProgressRepo:
    def __init__(self, events: list[_ProgressEvent]) -> None:
        self.events = events

    async def list_after_sequence(
        self,
        *,
        request_id: int,
        sequence: int,
        limit: int = 100,
    ) -> list[_ProgressEvent]:
        del limit
        return [
            event
            for event in self.events
            if event.request_id == request_id and event.sequence > sequence
        ]

    async def sequence_for_event_id(self, *, request_id: int, event_id: str) -> int | None:
        for event in self.events:
            if event.request_id == request_id and event.event_id == event_id:
                return event.sequence
        return None


def _durable_events(request_id: int) -> list[_ProgressEvent]:
    return [
        _ProgressEvent(
            event_id=f"evt-{request_id}-1",
            request_id=request_id,
            sequence=1,
            kind="stage",
            stage="queued",
            status="pending",
        ),
        _ProgressEvent(
            event_id=f"evt-{request_id}-2",
            request_id=request_id,
            sequence=2,
            kind="stage",
            stage="summarizing",
            status="running",
        ),
        _ProgressEvent(
            event_id=f"evt-{request_id}-3",
            request_id=request_id,
            sequence=3,
            kind="done",
            stage="done",
            status="succeeded",
        ),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_stream_returns_401_without_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _build_app_and_client(monkeypatch)
    response = client.get("/v1/requests/999/stream")
    assert response.status_code == 401


def test_stream_returns_403_for_foreign_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid token for a user who does not own the request returns 403."""
    from app.adapters.content.streaming.events import DonePayload, StreamEvent
    from app.api.routers.auth.tokens import create_access_token

    request_id = 99999
    # Pre-load a done event so the SSE generator exits quickly even if accessed by owner.
    hub = _make_hub(
        StreamEvent.now("done", DonePayload(summary_id=None, request_id=str(request_id)), "c"),
        request_id=str(request_id),
    )
    _patch_hub(monkeypatch, hub)

    # Client is _OTHER_ID; owner check only passes for _OWNER_ID → 403.
    client = _build_app_and_client(monkeypatch, owner_id=_OWNER_ID)
    token = create_access_token(_OTHER_ID, client_id="test")

    response = client.get(
        f"/v1/requests/{request_id}/stream",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_stream_returns_200_with_event_stream_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorized request returns 200 with text/event-stream content type.

    We open the stream and close immediately after confirming status+headers
    to avoid blocking on the SSE heartbeat loop.
    """
    from app.adapters.content.streaming.events import DonePayload, StreamEvent
    from app.api.routers.auth.tokens import create_access_token

    request_id = 11111
    done_ev = StreamEvent.now(
        "done",
        DonePayload(summary_id=None, request_id=str(request_id)),
        "test-corr",
    )
    hub = _make_hub(done_ev, request_id=str(request_id))
    _patch_hub(monkeypatch, hub)

    token = create_access_token(_OWNER_ID, client_id="test")
    client = _build_app_and_client(monkeypatch)

    with client.stream(
        "GET",
        f"/v1/requests/{request_id}/stream",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        assert response.status_code == 200
        content_type = response.headers.get("content-type", "")
        assert "text/event-stream" in content_type
        # Close immediately — don't iterate lines to avoid blocking on heartbeats.
        response.close()


def test_stream_delivers_events_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """stage + section + done events arrive in order over the SSE stream.

    We drain lines until we see a 'done' event line, then stop.  This avoids
    blocking on the SSE heartbeat that fires after the terminal event.
    """
    from app.adapters.content.streaming.events import (
        DonePayload,
        SectionPayload,
        StagePayload,
        StreamEvent,
    )
    from app.api.routers.auth.tokens import create_access_token

    request_id = 22222
    stage_ev = StreamEvent.now("stage", StagePayload(stage=ProcessingStage.SUMMARIZING), "cid")
    section_ev = StreamEvent.now(
        "section",
        SectionPayload(section="tldr", content="Quick summary"),
        "cid",
    )
    done_ev = StreamEvent.now(
        "done",
        DonePayload(summary_id="42", request_id=str(request_id)),
        "cid",
    )

    hub = _make_hub(stage_ev, section_ev, done_ev, request_id=str(request_id))
    _patch_hub(monkeypatch, hub)

    token = create_access_token(_OWNER_ID, client_id="test")
    client = _build_app_and_client(monkeypatch)

    raw_lines: list[str] = []
    with client.stream(
        "GET",
        f"/v1/requests/{request_id}/stream",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        assert response.status_code == 200
        content_type = response.headers.get("content-type", "")
        assert "text/event-stream" in content_type
        # Drain lines only until we see the done event, then close immediately.
        for line in response.iter_lines():
            raw_lines.append(line)
            if line == "event: done":
                response.close()
                break

    event_types = _parse_event_types(raw_lines)

    assert "stage" in event_types
    assert "section" in event_types
    assert "done" in event_types

    # Order must be preserved.
    assert event_types.index("stage") < event_types.index("section") < event_types.index("done")


def test_stream_replays_durable_events_after_last_event_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routers.auth.tokens import create_access_token

    request_id = 44444
    events = _durable_events(request_id)
    client = _build_app_and_client(monkeypatch, progress_repo=_ProgressRepo(events))
    token = create_access_token(_OWNER_ID, client_id="test")

    raw_lines: list[str] = []
    with client.stream(
        "GET",
        f"/v1/requests/{request_id}/stream",
        headers={
            "Authorization": f"Bearer {token}",
            "Last-Event-ID": events[0].event_id,
        },
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            raw_lines.append(line)
            if line == "event: done":
                response.close()
                break

    assert f"id: {events[0].event_id}" not in raw_lines
    assert f"id: {events[1].event_id}" in raw_lines
    assert f"id: {events[2].event_id}" in raw_lines


def test_stream_uses_durable_repo_without_shared_stream_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routers.auth.tokens import create_access_token

    request_id = 55555
    client = _build_app_and_client(
        monkeypatch,
        progress_repo=_ProgressRepo(_durable_events(request_id)),
    )
    token = create_access_token(_OWNER_ID, client_id="test")

    raw_lines: list[str] = []
    with client.stream(
        "GET",
        f"/v1/requests/{request_id}/stream",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            raw_lines.append(line)
            if line == "event: done":
                response.close()
                break

    assert _parse_event_types(raw_lines) == ["stage", "stage", "done"]


def test_stream_replays_terminal_durable_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routers.auth.tokens import create_access_token

    request_id = 66666
    terminal = _ProgressEvent(
        event_id=f"evt-{request_id}-9",
        request_id=request_id,
        sequence=9,
        kind="done",
        stage="done",
        status="succeeded",
    )
    client = _build_app_and_client(monkeypatch, progress_repo=_ProgressRepo([terminal]))
    token = create_access_token(_OWNER_ID, client_id="test")

    raw_lines: list[str] = []
    with client.stream(
        "GET",
        f"/v1/requests/{request_id}/stream?since_sequence=8",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            raw_lines.append(line)
            if line == "event: done":
                response.close()
                break

    assert raw_lines.count("event: done") == 1
    assert f"id: {terminal.event_id}" in raw_lines


def test_disconnect_mid_stream_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Client disconnect mid-SSE-stream does not raise or corrupt state.

    We pre-load a stage event followed by a done event.  The test opens the
    stream, reads until it sees the stage event, then closes immediately
    (before the done event is consumed).  This exercises the
    CancelledError/GeneratorExit path in the event_generator without causing
    the server-side coroutine to block indefinitely waiting for a terminal
    event that never arrives.

    Note: starlette's TestClient ASGI transport does not send an
    ``http.disconnect`` message when the client closes, so we cannot test
    _listen_for_disconnect directly here.  Instead we verify that closing
    mid-iteration does not propagate an exception to the test.
    """
    from app.adapters.content.streaming.events import (
        DonePayload,
        StagePayload,
        StreamEvent,
    )
    from app.api.routers.auth.tokens import create_access_token

    request_id = 33333
    stage_ev = StreamEvent.now("stage", StagePayload(stage=ProcessingStage.SUMMARIZING), "c")
    done_ev = StreamEvent.now("done", DonePayload(summary_id=None, request_id=str(request_id)), "c")

    hub = _make_hub(stage_ev, done_ev, request_id=str(request_id))
    _patch_hub(monkeypatch, hub)

    token = create_access_token(_OWNER_ID, client_id="test")
    client = _build_app_and_client(monkeypatch)

    with client.stream(
        "GET",
        f"/v1/requests/{request_id}/stream",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        assert response.status_code == 200
        # Read until the first stage event line, then disconnect immediately.
        for line in response.iter_lines():
            if line == "event: stage":
                response.close()
                break

    # No exception raised; test passing is the assertion.
