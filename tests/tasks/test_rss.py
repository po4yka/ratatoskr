from types import SimpleNamespace

import pytest

from app.tasks import rss


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def __aenter__(self) -> "_FakeBot":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class _FakeDeliveryService:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def deliver_new_items(self, send_message: object, *, new_item_ids: list[int]) -> dict[str, int]:
        await send_message(10, f"items: {new_item_ids}")  # type: ignore[misc]
        return {"delivered": len(new_item_ids)}


class _FakeWorker:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.limits: list[int] = []

    async def run_once(self, *, limit: int | None = None) -> dict[str, int]:
        self.limits.append(limit or 0)
        if self.exc:
            raise self.exc
        return {"processed": 1}


class _FakeRunner:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.called = False

    async def run_once(self) -> dict[str, int]:
        self.called = True
        if self.exc:
            raise self.exc
        return {"sources": 1}


class _FakeRuntime:
    def __init__(self, *, worker: _FakeWorker | None = None, runner: _FakeRunner | None = None) -> None:
        self.worker = worker or _FakeWorker()
        self.runner = runner or _FakeRunner()
        self.delivery = _FakeDeliveryService()
        self.bot = _FakeBot()

    def create_signal_ingestion_worker(self) -> _FakeWorker:
        return self.worker

    def create_source_ingestion_runner(self) -> _FakeRunner:
        return self.runner

    def create_delivery_service(self) -> _FakeDeliveryService:
        return self.delivery

    def create_bot_client(self) -> _FakeBot:
        return self.bot


def _cfg(*, rss_enabled: bool = True, auto_summarize: bool = True, signals: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        rss=SimpleNamespace(
            enabled=rss_enabled,
            auto_summarize=auto_summarize,
            max_items_per_poll=5,
        ),
        signal_ingestion=SimpleNamespace(any_enabled=signals),
    )


@pytest.mark.asyncio
async def test_run_signal_ingestion_skips_when_disabled() -> None:
    runtime = _FakeRuntime()

    await rss._run_signal_ingestion(_cfg(signals=False), runtime, "cid")

    assert runtime.worker.limits == []


@pytest.mark.asyncio
async def test_run_signal_ingestion_logs_worker_errors() -> None:
    runtime = _FakeRuntime(worker=_FakeWorker(RuntimeError("down")))

    await rss._run_signal_ingestion(_cfg(signals=True), runtime, "cid")

    assert runtime.worker.limits == [5]


@pytest.mark.asyncio
async def test_run_optional_source_ingestors_skips_and_handles_errors() -> None:
    skipped_runtime = _FakeRuntime()
    await rss._run_optional_source_ingestors(_cfg(signals=False), skipped_runtime, "cid")
    assert not skipped_runtime.runner.called

    failing_runtime = _FakeRuntime(runner=_FakeRunner(RuntimeError("down")))
    await rss._run_optional_source_ingestors(_cfg(signals=True), failing_runtime, "cid")
    assert failing_runtime.runner.called


@pytest.mark.asyncio
async def test_rss_poll_body_delivers_new_items(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _FakeRuntime()

    async def fake_poll_all_feeds(db: object) -> dict[str, object]:
        return {"new_item_ids": [1, 2], "polled": 1, "new_items": 2, "errors": 0}

    monkeypatch.setattr("app.adapters.rss.feed_poller.poll_all_feeds", fake_poll_all_feeds)
    monkeypatch.setattr(rss, "build_rss_poll_task_runtime", lambda cfg, db: runtime)

    await rss._rss_poll_body(_cfg(), object())  # type: ignore[arg-type]

    assert runtime.worker.limits == [5]
    assert runtime.runner.called
    assert runtime.bot.sent == [(10, "items: [1, 2]")]


@pytest.mark.asyncio
async def test_rss_poll_body_swallows_poll_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_poll_all_feeds(db: object) -> dict[str, object]:
        raise RuntimeError("down")

    monkeypatch.setattr("app.adapters.rss.feed_poller.poll_all_feeds", fake_poll_all_feeds)
    monkeypatch.setattr(rss, "build_rss_poll_task_runtime", lambda cfg, db: _FakeRuntime())

    await rss._rss_poll_body(_cfg(), object())  # type: ignore[arg-type]
