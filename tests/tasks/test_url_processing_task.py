"""Tests for app.tasks.url_processing — the Taskiq URL-processing task body.

Covers:
- Happy path: lease acquired, URLProcessor succeeds, summary edited, job marked succeeded
- Lease not acquired: task returns early without calling the processor
- Idempotency: if summary already exists when the task runs, LLM is skipped
- Failure path: job is marked failed and placeholder is edited with error text
- Canonical card formatting: _format_summary_for_edit uses build_card_sections output
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Taskiq stub (keeps tests independent of taskiq/taskiq_redis install) ──────


def _stub_taskiq(monkeypatch):
    for mod_name in (
        "taskiq",
        "taskiq.abc",
        "taskiq.abc.schedule_source",
        "taskiq.scheduler",
        "taskiq.scheduler.scheduled_task",
        "taskiq.message",
        "taskiq_redis",
    ):
        if mod_name not in sys.modules:
            monkeypatch.setitem(sys.modules, mod_name, types.ModuleType(mod_name))

    taskiq_mod = sys.modules["taskiq"]
    taskiq_mod.AsyncBroker = object
    taskiq_mod.TaskiqDepends = lambda fn, **_kw: None
    taskiq_mod.TaskiqMiddleware = object
    taskiq_mod.InMemoryBroker = MagicMock

    msg_mod = sys.modules["taskiq.message"]
    msg_mod.TaskiqMessage = object

    sched_task_mod = sys.modules["taskiq.scheduler.scheduled_task"]
    sched_task_mod.ScheduledTask = MagicMock

    source_mod = sys.modules["taskiq.abc.schedule_source"]
    source_mod.ScheduleSource = object

    tkr_mod = sys.modules["taskiq_redis"]
    tkr_mod.RedisStreamBroker = MagicMock
    tkr_mod.RedisAsyncResultBackend = MagicMock


# ── Minimal config stub ───────────────────────────────────────────────────────


def _build_cfg():
    return SimpleNamespace(
        runtime=SimpleNamespace(
            url_flow_lease_ttl_sec=900,
            max_concurrent_calls=2,
            url_worker_enqueue_enabled=True,
            url_worker_concurrency=4,
            preferred_lang="en",
            debug_payloads=False,
        ),
        background=SimpleNamespace(
            durable_retry_delay_seconds=30,
        ),
        telegram=SimpleNamespace(bot_token="123:tok"),
        openrouter=SimpleNamespace(api_key="k", model="m", fallback_models=[]),
        scraper=SimpleNamespace(firecrawl_self_hosted_enabled=False),
        attachment=SimpleNamespace(article_vision_min_images=3),
        telegram_limits=SimpleNamespace(max_message_length=4096),
    )


# ── Leased job stub ───────────────────────────────────────────────────────────


def _make_leased_job(request_id: int = 1, cid: str = "test-cid"):
    return SimpleNamespace(
        id=10,
        request_id=request_id,
        attempt_count=1,
        max_attempts=3,
        correlation_id=cid,
    )


# ── Runtime stub helper ───────────────────────────────────────────────────────


def _make_runtime(url_processor=None, telegram_sender=None, response_formatter=None):
    """Build a minimal URLProcessingTaskRuntime-shaped namespace for tests."""
    if url_processor is None:
        url_processor = MagicMock()
        url_processor.handle_url_flow = AsyncMock()
    if telegram_sender is None:
        telegram_sender = MagicMock()
        telegram_sender.edit_message_text = AsyncMock()
    if response_formatter is None:
        # Minimal stub: build_card_sections uses _text_processor, _data_formatter, _lang
        text_proc = MagicMock()
        text_proc.sanitize_summary_text = lambda x: x
        data_fmt = MagicMock()
        data_fmt.format_key_stats_compact = MagicMock(return_value=[])
        response_formatter = MagicMock()
        response_formatter._text_processor = text_proc
        response_formatter._data_formatter = data_fmt
        response_formatter._lang = "en"
    return SimpleNamespace(
        url_processor=url_processor,
        telegram_sender=telegram_sender,
        response_formatter=response_formatter,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_url_request_lease_not_acquired(monkeypatch):
    """When lease_next returns None the task returns without processing."""
    _stub_taskiq(monkeypatch)
    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)

    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    job_repo = MagicMock()
    job_repo.lease_next = AsyncMock(return_value=None)

    runtime = _make_runtime()

    from app.tasks.url_processing import _process_url_request_body

    with patch(
        "app.api.background.durable_jobs.RequestProcessingJobRepository",
        new=MagicMock(return_value=job_repo),
    ):
        await _process_url_request_body(
            request_id=42,
            cfg=_build_cfg(),
            db=MagicMock(),
            runtime=runtime,
        )

    runtime.url_processor.handle_url_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_url_request_happy_path(monkeypatch):
    """Happy path: lease acquired, URLProcessor succeeds, placeholder edited, job succeeded."""
    _stub_taskiq(monkeypatch)
    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)

    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    leased_job = _make_leased_job(request_id=1, cid="cid-happy")
    job_repo = MagicMock()
    job_repo.lease_next = AsyncMock(return_value=leased_job)
    job_repo.mark_succeeded = AsyncMock()
    job_repo.mark_failed = AsyncMock()

    from app.adapters.content.url_flow_models import URLProcessingFlowResult

    url_processor = MagicMock()
    url_processor.handle_url_flow = AsyncMock(
        return_value=URLProcessingFlowResult(
            success=True,
            summary_json={"title": "T", "tldr": "TL", "summary_250": "S"},
        )
    )
    telegram_sender = MagicMock()
    telegram_sender.edit_message_text = AsyncMock()
    runtime = _make_runtime(url_processor=url_processor, telegram_sender=telegram_sender)

    request_data = {
        "chat_id": 100,
        "input_url": "https://example.com",
        "bot_reply_message_id": 55,
        "correlation_id": "cid-happy",
    }
    # First _load_summary returns None (not done yet); second returns data after processing.
    summary_after = {"json_payload": {"title": "T", "tldr": "TL", "summary_250": "S"}}

    from app.tasks.url_processing import _process_url_request_body

    with (
        patch(
            "app.api.background.durable_jobs.RequestProcessingJobRepository",
            new=MagicMock(return_value=job_repo),
        ),
        patch(
            "app.tasks.url_processing._load_request",
            new=AsyncMock(return_value=request_data),
        ),
        patch(
            "app.tasks.url_processing._load_summary",
            new=AsyncMock(side_effect=[None, summary_after]),
        ),
    ):
        await _process_url_request_body(
            request_id=1,
            cfg=_build_cfg(),
            db=MagicMock(),
            runtime=runtime,
        )

    url_processor.handle_url_flow.assert_awaited_once()
    telegram_sender.edit_message_text.assert_awaited_once()
    job_repo.mark_succeeded.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_url_request_idempotent_when_summary_exists(monkeypatch):
    """When summary already exists, URLProcessor is not called again."""
    _stub_taskiq(monkeypatch)
    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)

    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    leased_job = _make_leased_job(request_id=2, cid="cid-idem")
    job_repo = MagicMock()
    job_repo.lease_next = AsyncMock(return_value=leased_job)
    job_repo.mark_succeeded = AsyncMock()

    url_processor = MagicMock()
    url_processor.handle_url_flow = AsyncMock()
    telegram_sender = MagicMock()
    telegram_sender.edit_message_text = AsyncMock()
    runtime = _make_runtime(url_processor=url_processor, telegram_sender=telegram_sender)

    request_data = {
        "chat_id": 100,
        "input_url": "https://example.com",
        "bot_reply_message_id": 66,
        "correlation_id": "cid-idem",
    }
    existing_summary = {"json_payload": {"title": "Cached", "tldr": "c", "summary_250": "cs"}}

    from app.tasks.url_processing import _process_url_request_body

    with (
        patch(
            "app.api.background.durable_jobs.RequestProcessingJobRepository",
            new=MagicMock(return_value=job_repo),
        ),
        patch(
            "app.tasks.url_processing._load_request",
            new=AsyncMock(return_value=request_data),
        ),
        patch(
            "app.tasks.url_processing._load_summary",
            new=AsyncMock(return_value=existing_summary),
        ),
    ):
        await _process_url_request_body(
            request_id=2,
            cfg=_build_cfg(),
            db=MagicMock(),
            runtime=runtime,
        )

    url_processor.handle_url_flow.assert_not_awaited()
    telegram_sender.edit_message_text.assert_awaited_once()
    job_repo.mark_succeeded.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_url_request_marks_failed_on_exception(monkeypatch):
    """When URLProcessor raises, job is marked failed and placeholder edited with error."""
    _stub_taskiq(monkeypatch)
    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)

    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    leased_job = _make_leased_job(request_id=3, cid="cid-fail")
    job_repo = MagicMock()
    job_repo.lease_next = AsyncMock(return_value=leased_job)
    job_repo.mark_failed = AsyncMock(return_value="failed")
    job_repo.mark_succeeded = AsyncMock()

    url_processor = MagicMock()
    url_processor.handle_url_flow = AsyncMock(side_effect=RuntimeError("scraper blew up"))
    telegram_sender = MagicMock()
    telegram_sender.edit_message_text = AsyncMock()
    runtime = _make_runtime(url_processor=url_processor, telegram_sender=telegram_sender)

    request_data = {
        "chat_id": 100,
        "input_url": "https://example.com",
        "bot_reply_message_id": 77,
        "correlation_id": "cid-fail",
    }

    from app.tasks.url_processing import _process_url_request_body

    with (
        patch(
            "app.api.background.durable_jobs.RequestProcessingJobRepository",
            new=MagicMock(return_value=job_repo),
        ),
        patch(
            "app.tasks.url_processing._load_request",
            new=AsyncMock(return_value=request_data),
        ),
        patch(
            "app.tasks.url_processing._load_summary",
            new=AsyncMock(return_value=None),
        ),
    ):
        await _process_url_request_body(
            request_id=3,
            cfg=_build_cfg(),
            db=MagicMock(),
            runtime=runtime,
        )

    job_repo.mark_failed.assert_awaited_once()
    job_repo.mark_succeeded.assert_not_awaited()
    # Placeholder edit with error text is best-effort so we check it was called.
    telegram_sender.edit_message_text.assert_awaited()


def test_format_summary_uses_card_sections_canonical_output(monkeypatch):
    """_format_summary_for_edit produces the canonical card text via build_card_sections."""
    _stub_taskiq(monkeypatch)
    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)

    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    summary_json = {
        "tldr": "The article argues X.",
        "summary_250": "A longer summary.",
        "metadata": {"title": "Test Article", "canonical_url": "https://example.com/a", "domain": "example.com"},
        "key_ideas": ["Idea one", "Idea two"],
    }

    # Build a real ResponseFormatter (no Telegram client) to test the actual code path.
    from app.adapters.external.formatting.data_formatter import DataFormatterImpl
    from app.adapters.external.formatting.message_validator import MessageValidatorImpl
    from app.adapters.external.formatting.response_sender import ResponseSenderImpl
    from app.adapters.external.formatting.text_processor import TextProcessorImpl

    validator = MessageValidatorImpl()
    sender = ResponseSenderImpl(validator)
    text_proc = TextProcessorImpl(sender)
    data_fmt = DataFormatterImpl(lang="en")

    rf = MagicMock()
    rf._text_processor = text_proc
    rf._data_formatter = data_fmt
    rf._lang = "en"

    runtime = _make_runtime(response_formatter=rf)

    from app.tasks.url_processing import _format_summary_for_edit

    result = _format_summary_for_edit(
        {"json_payload": summary_json},
        cid="test-cid",
        runtime=runtime,
    )

    # The result should contain the title and tldr from the canonical card.
    assert "Test Article" in result
    assert "The article argues X." in result
    # Should NOT be the fallback placeholder.
    assert "Summary ready" not in result
