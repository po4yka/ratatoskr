from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from app.api.background.durable_jobs import (
    DurableRequestProcessingQueue,
    LeasedRequestJob,
    RequestProcessingJobRepository,
)
from app.api.background.progress import BackgroundProgressPublisher
from app.api.background.progress_events import ProgressEventRecord
from app.api.background_tasks import process_url_request
from app.api.models.requests import SubmitURLRequest
from app.api.routers.content.requests import submit_request
from app.api.routers.content.streams import stream_request
from app.application.dto.request_workflow import RequestCreatedDTO
from app.core.time_utils import UTC
from app.db.models import RequestProcessingJob


class FakeJobRepository:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []
        self.leased: list[LeasedRequestJob] = []
        self.succeeded: list[int] = []
        self.finalized_requests: list[int] = []
        self.failed: list[dict[str, Any]] = []
        self.summary_exists = False
        self.request_status: tuple[str | None, str | None] = ("pending", None)
        self.requeue_count = 0
        self.dead_letter_count = 0
        self.stuck_count = 0

    async def enqueue(
        self,
        *,
        request_id: int,
        correlation_id: str | None,
        max_attempts: int,
    ) -> dict[str, Any]:
        payload = {
            "request_id": request_id,
            "correlation_id": correlation_id,
            "max_attempts": max_attempts,
            "status": "queued",
        }
        self.enqueued.append(payload)
        return payload

    async def lease_next(
        self,
        *,
        lease_owner: str,
        lease_ttl_seconds: int,
    ) -> LeasedRequestJob | None:
        return self.leased.pop(0) if self.leased else None

    async def mark_succeeded(
        self,
        job_id: int,
        *,
        lease_owner: str,
        request_id: int | None = None,
    ) -> None:
        self.succeeded.append(job_id)
        if request_id is not None:
            self.finalized_requests.append(request_id)

    async def mark_failed(
        self,
        job: LeasedRequestJob,
        *,
        lease_owner: str,
        error_code: str,
        error_message: str,
        retry_delay_seconds: int,
    ) -> str:
        status = "dead_letter" if job.attempt_count >= job.max_attempts else "failed"
        self.failed.append(
            {
                "job_id": job.id,
                "status": status,
                "error_code": error_code,
                "error_message": error_message,
            }
        )
        return status

    async def requeue_expired_leases(self) -> int:
        return self.requeue_count

    async def dead_letter_exhausted(self) -> int:
        return self.dead_letter_count

    async def reconcile_stuck_processing_requests(
        self,
        *,
        older_than_seconds: int,
        max_attempts: int,
    ) -> int:
        return self.stuck_count

    async def has_summary(self, request_id: int) -> bool:
        return self.summary_exists

    async def get_request_status(self, request_id: int) -> tuple[str | None, str | None]:
        return self.request_status


class FakeTransaction:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeSession:
    def __init__(self, *, scalar_results: list[Any] | None = None) -> None:
        self.scalar_results = scalar_results or []
        self.executed: list[Any] = []
        self.flush_count = 0

    async def scalar(self, statement: Any) -> Any:
        self.executed.append(statement)
        return self.scalar_results.pop(0) if self.scalar_results else None

    async def execute(self, statement: Any) -> Any:
        self.executed.append(statement)
        return SimpleNamespace(rowcount=1)

    async def flush(self) -> None:
        self.flush_count += 1


class FakeDatabase:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self._session)

    def session(self) -> FakeTransaction:
        return FakeTransaction(self._session)


class FakeProcessor:
    def __init__(self, repo: FakeJobRepository, *, fail: bool = False) -> None:
        self._repo = repo
        self._fail = fail
        self.calls: list[dict[str, Any]] = []

    async def execute_request(self, request_id: int, *, correlation_id: str | None = None) -> None:
        self.calls.append({"request_id": request_id, "correlation_id": correlation_id})
        if self._fail:
            raise RuntimeError("processor crashed")
        self._repo.summary_exists = True
        self._repo.request_status = ("success", None)


class ProgressWritingProcessor(FakeProcessor):
    def __init__(
        self,
        repo: FakeJobRepository,
        publisher: BackgroundProgressPublisher,
    ) -> None:
        super().__init__(repo)
        self._publisher = publisher

    async def execute_request(self, request_id: int, *, correlation_id: str | None = None) -> None:
        await super().execute_request(request_id, correlation_id=correlation_id)
        await self._publisher.publish(
            request_id=request_id,
            status="PROCESSING",
            stage="SUMMARIZATION",
            message="Summarizing content...",
            progress=0.5,
            correlation_id=correlation_id,
        )


class FakeProgressEventRepository:
    def __init__(self) -> None:
        self.appended: list[dict[str, Any]] = []

    async def append(self, **kwargs: Any) -> ProgressEventRecord:
        self.appended.append(kwargs)
        return ProgressEventRecord(
            event_id="event-1",
            request_id=kwargs["request_id"],
            sequence=1,
            kind=kwargs["kind"],
            stage=kwargs["stage"],
            status=kwargs["status"],
            message=kwargs["message"],
            progress=kwargs["progress"],
            payload=kwargs["payload"],
            created_at="2026-05-21T00:00:00Z",
            correlation_id=kwargs["correlation_id"],
        )


class FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))


class FakeRequestService:
    async def check_duplicate_url(self, user_id: int, url: str) -> None:
        return None

    async def create_url_request(
        self,
        user_id: int,
        input_url: str,
        lang_preference: str = "auto",
    ) -> RequestCreatedDTO:
        return RequestCreatedDTO(
            id=42,
            type="url",
            status="pending",
            correlation_id="cid-submit",
            created_at=datetime.now(UTC),
            input_url=input_url,
            normalized_url=input_url,
        )


class FakeStreamRequestService:
    async def get_request_by_id(self, user_id: int, request_id: int) -> dict[str, int]:
        return {"user_id": user_id, "request_id": request_id}


class FakeStreamRequest:
    async def is_disconnected(self) -> bool:
        return False


class FakeReplayProgressEventRepository:
    def __init__(self) -> None:
        self.sequence_lookups: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.events = [
            ProgressEventRecord(
                event_id="event-2",
                request_id=42,
                sequence=2,
                kind="stage",
                stage="summarizing",
                status="running",
                message="Summarizing content...",
                progress=0.5,
                payload={"step": "summarize"},
                created_at="2026-05-21T00:00:02Z",
                correlation_id="cid-replay",
            ),
            ProgressEventRecord(
                event_id="event-3",
                request_id=42,
                sequence=3,
                kind="done",
                stage="completed",
                status="succeeded",
                message="Summary ready",
                progress=1.0,
                payload={"summary_id": 99},
                created_at="2026-05-21T00:00:03Z",
                correlation_id="cid-replay",
            ),
        ]

    async def sequence_for_event_id(self, *, request_id: int, event_id: str) -> int | None:
        self.sequence_lookups.append({"request_id": request_id, "event_id": event_id})
        return 1 if event_id == "event-1" else None

    async def list_after_sequence(
        self,
        *,
        request_id: int,
        sequence: int,
        limit: int = 100,
    ) -> list[ProgressEventRecord]:
        self.list_calls.append({"request_id": request_id, "sequence": sequence, "limit": limit})
        return [event for event in self.events if event.sequence > sequence]


def _queue(repo: FakeJobRepository, processor: FakeProcessor) -> DurableRequestProcessingQueue:
    return DurableRequestProcessingQueue(
        repository=repo,
        processor=processor,
        max_attempts=3,
        lease_ttl_seconds=30,
        retry_delay_seconds=1,
        poll_interval_seconds=0.01,
        stale_processing_seconds=60,
    )


@pytest.mark.asyncio
async def test_submit_request_creates_durable_job() -> None:
    repo = FakeJobRepository()
    queue = _queue(repo, FakeProcessor(repo))
    app = SimpleNamespace(
        state=SimpleNamespace(runtime=SimpleNamespace(durable_request_queue=queue))
    )
    request = Request({"type": "http", "method": "POST", "path": "/v1/requests", "app": app})

    response = await submit_request(
        request,
        SubmitURLRequest(input_url="https://example.com/article"),
        user={"user_id": 7},
        request_service=FakeRequestService(),  # type: ignore[arg-type]
    )

    assert response["success"] is True
    assert repo.enqueued == [
        {"request_id": 42, "correlation_id": "cid-submit", "max_attempts": 3, "status": "queued"}
    ]


@pytest.mark.asyncio
async def test_legacy_background_helper_enqueues_durable_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeJobRepository()
    queue = _queue(repo, FakeProcessor(repo))
    monkeypatch.setattr(
        "app.api.background_tasks.get_current_api_runtime",
        lambda: SimpleNamespace(durable_request_queue=queue),
    )

    await process_url_request(42, correlation_id="cid-helper")

    assert repo.enqueued == [
        {"request_id": 42, "correlation_id": "cid-helper", "max_attempts": 3, "status": "queued"}
    ]


@pytest.mark.asyncio
async def test_repository_enqueue_returns_durable_job_model() -> None:
    job = RequestProcessingJob(
        id=1,
        request_id=42,
        status="queued",
        attempt_count=0,
        max_attempts=3,
        correlation_id="cid-repo",
    )
    session = FakeSession(scalar_results=[job])
    repository = RequestProcessingJobRepository(FakeDatabase(session))

    result = await repository.enqueue(request_id=42, correlation_id="cid-repo", max_attempts=3)

    assert result["request_id"] == 42
    assert result["status"] == "queued"
    assert result["attempt_count"] == 0
    assert result["max_attempts"] == 3
    assert session.executed


@pytest.mark.asyncio
async def test_repository_lease_next_sets_running_lease_fields() -> None:
    job = RequestProcessingJob(
        id=7,
        request_id=42,
        status="queued",
        attempt_count=0,
        max_attempts=3,
        correlation_id="cid-lease",
    )
    session = FakeSession(scalar_results=[job])
    repository = RequestProcessingJobRepository(FakeDatabase(session))

    leased = await repository.lease_next(lease_owner="worker-1", lease_ttl_seconds=30)

    assert leased == LeasedRequestJob(7, 42, 1, 3, "cid-lease")
    assert job.status == "running"
    assert job.lease_owner == "worker-1"
    assert job.lease_expires_at is not None
    assert job.attempt_count == 1
    assert session.flush_count == 1


@pytest.mark.asyncio
async def test_worker_processes_job_successfully() -> None:
    repo = FakeJobRepository()
    repo.leased.append(LeasedRequestJob(1, 42, 1, 3, "cid-worker"))
    processor = FakeProcessor(repo)
    queue = _queue(repo, processor)

    assert await queue.run_once() is True

    assert processor.calls == [{"request_id": 42, "correlation_id": "cid-worker"}]
    assert repo.succeeded == [1]
    assert repo.failed == []


@pytest.mark.asyncio
async def test_worker_processing_persists_progress_events() -> None:
    event_repo = FakeProgressEventRepository()
    redis = FakeRedis()
    publisher = BackgroundProgressPublisher(
        redis=redis,
        logger=SimpleNamespace(
            warning=lambda *args, **kwargs: None, debug=lambda *args, **kwargs: None
        ),
        progress_event_repo=event_repo,
    )
    repo = FakeJobRepository()
    repo.leased.append(LeasedRequestJob(1, 42, 1, 3, "cid-worker-progress"))
    queue = _queue(repo, ProgressWritingProcessor(repo, publisher))

    assert await queue.run_once() is True

    assert repo.succeeded == [1]
    assert event_repo.appended[0]["request_id"] == 42
    assert event_repo.appended[0]["stage"] == "summarizing"
    assert event_repo.appended[0]["correlation_id"] == "cid-worker-progress"


@pytest.mark.asyncio
async def test_startup_reconciliation_requeues_crashed_running_job() -> None:
    repo = FakeJobRepository()
    repo.requeue_count = 1
    repo.stuck_count = 1
    queue = _queue(repo, FakeProcessor(repo))

    result = await queue.reconcile_startup()

    assert result == {"requeued": 1, "dead_lettered": 0, "stuck": 1}


@pytest.mark.asyncio
async def test_duplicate_delivery_finalizes_existing_summary_once() -> None:
    repo = FakeJobRepository()
    repo.summary_exists = True
    repo.leased.append(LeasedRequestJob(1, 42, 1, 3, "cid-dup"))
    processor = FakeProcessor(repo)
    queue = _queue(repo, processor)

    assert await queue.run_once() is True

    assert processor.calls == []
    assert repo.succeeded == [1]
    assert repo.finalized_requests == [42]
    assert repo.failed == []


@pytest.mark.asyncio
async def test_max_retry_transitions_to_dead_letter() -> None:
    repo = FakeJobRepository()
    repo.leased.append(LeasedRequestJob(1, 42, 3, 3, "cid-dead"))
    processor = FakeProcessor(repo, fail=True)
    queue = _queue(repo, processor)

    assert await queue.run_once() is True

    assert repo.failed == [
        {
            "job_id": 1,
            "status": "dead_letter",
            "error_code": "RuntimeError",
            "error_message": "processor crashed",
        }
    ]


def test_request_processing_job_model_contains_durable_state_columns() -> None:
    columns = set(RequestProcessingJob.__table__.columns.keys())

    assert {
        "request_id",
        "status",
        "attempt_count",
        "max_attempts",
        "lease_owner",
        "lease_expires_at",
        "retry_after",
        "last_error_code",
        "last_error_message",
        "correlation_id",
        "created_at",
        "updated_at",
    }.issubset(columns)


@pytest.mark.asyncio
async def test_background_progress_publisher_persists_event_and_publishes_same_shape() -> None:
    event_repo = FakeProgressEventRepository()
    redis = FakeRedis()
    publisher = BackgroundProgressPublisher(
        redis=redis,
        logger=SimpleNamespace(
            warning=lambda *args, **kwargs: None, debug=lambda *args, **kwargs: None
        ),
        progress_event_repo=event_repo,
    )

    await publisher.publish(
        request_id=42,
        status="PROCESSING",
        stage="SUMMARIZATION",
        message="Summarizing content...",
        progress=0.5,
        correlation_id="cid-progress",
    )

    assert event_repo.appended[0]["request_id"] == 42
    assert event_repo.appended[0]["kind"] == "stage"
    assert event_repo.appended[0]["stage"] == "summarizing"
    assert event_repo.appended[0]["status"] == "running"
    assert event_repo.appended[0]["correlation_id"] == "cid-progress"
    channel, payload = redis.published[0]
    assert channel == "processing:request:42"
    assert '"event_id":"event-1"' in payload
    assert '"sequence":1' in payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "stage", "expected_kind", "expected_status", "expected_stage"),
    [
        ("COMPLETED", "DONE", "done", "succeeded", "done"),
        ("ERROR", "UNKNOWN", "error", "failed", "done"),
        ("CANCELLED", "CANCELLED", "error", "cancelled", "done"),
        ("PROCESSING", "VALIDATION", "stage", "running", "validating"),
    ],
)
async def test_progress_publisher_uses_shared_public_lifecycle_mapping(
    status: str,
    stage: str,
    expected_kind: str,
    expected_status: str,
    expected_stage: str,
) -> None:
    event_repo = FakeProgressEventRepository()
    publisher = BackgroundProgressPublisher(
        redis=None,
        logger=SimpleNamespace(
            warning=lambda *args, **kwargs: None, debug=lambda *args, **kwargs: None
        ),
        progress_event_repo=event_repo,
    )

    await publisher.publish(
        request_id=43,
        status=status,
        stage=stage,
        message="Lifecycle update",
        progress=1.0,
        correlation_id="cid-progress-map",
    )

    appended = event_repo.appended[0]
    assert appended["kind"] == expected_kind
    assert appended["status"] == expected_status
    assert appended["stage"] == expected_stage


@pytest.mark.asyncio
async def test_sse_replay_honors_since_sequence_and_last_event_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def capture_event_source_response(event_generator: Any) -> Any:
        captured["event_generator"] = event_generator
        return SimpleNamespace(kind="captured-sse-response")

    monkeypatch.setattr(
        "app.api.routers.content.streams._event_source_response",
        capture_event_source_response,
    )
    repo = FakeReplayProgressEventRepository()

    response = await stream_request(
        request_id=42,
        fastapi_request=FakeStreamRequest(),  # type: ignore[arg-type]
        since_sequence=0,
        last_event_id="event-1",
        user={"user_id": 7},
        request_service=FakeStreamRequestService(),  # type: ignore[arg-type]
        progress_event_repo=repo,
    )

    assert response.kind == "captured-sse-response"
    events = []
    async for event in captured["event_generator"]:
        events.append(event)

    assert repo.sequence_lookups == [{"request_id": 42, "event_id": "event-1"}]
    assert repo.list_calls[0] == {"request_id": 42, "sequence": 1, "limit": 100}
    assert [event["id"] for event in events] == ["event-2", "event-3"]
    assert [event["event"] for event in events] == ["stage", "done"]
    assert '"sequence":2' in events[0]["data"]
    assert '"summary_id":99' in events[1]["data"]
