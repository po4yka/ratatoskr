"""The sweep that re-drives jobs nothing else comes back for.

A failed Telegram URL job is parked at ``failed`` with a backoff and attempts
still on the clock -- and then nobody acts on it. The task is kicked once at
enqueue, taskiq's retry middleware never fires because the body handles its own
failures, and the API's polling queue refuses Telegram-owned rows by design. So
``max_attempts`` was never more than one attempt in practice.

An import job that dies without raising -- an OOM kill -- has the same shape from
the other direction: no lease, no TTL, no attempt counter, so ``processing``
never ends.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.tasks.job_reaper import _fail_stale_imports, _reap_body, _requeue_due_url_requests


@pytest.fixture
def cfg() -> Any:
    return SimpleNamespace(background=SimpleNamespace(stuck_processing_seconds=900))


def _patch_repos(
    monkeypatch: pytest.MonkeyPatch,
    *,
    retryable: list[int] | Exception = (),
    reaped_imports: list[int] | Exception = (),
) -> dict[str, Any]:
    """Stand in for both repositories, which the sweep imports lazily."""
    calls: dict[str, Any] = {}

    class _RequestRepo:
        def __init__(self, _db: Any) -> None:
            pass

        async def list_retryable_telegram_requests(self, *, limit: int) -> list[int]:
            calls["limit"] = limit
            if isinstance(retryable, Exception):
                raise retryable
            return list(retryable)

    class _ImportRepo:
        def __init__(self, _db: Any) -> None:
            pass

        async def async_fail_stale_processing(self, *, older_than_seconds: int) -> list[int]:
            calls["older_than_seconds"] = older_than_seconds
            if isinstance(reaped_imports, Exception):
                raise reaped_imports
            return list(reaped_imports)

    monkeypatch.setattr(
        "app.infrastructure.persistence.request_processing_job_repository."
        "RequestProcessingJobRepository",
        _RequestRepo,
    )
    monkeypatch.setattr(
        "app.infrastructure.persistence.repositories.import_job_repository."
        "ImportJobRepositoryAdapter",
        _ImportRepo,
    )
    return calls


def _patch_kiq(monkeypatch: pytest.MonkeyPatch, *, fail_on: set[int] | None = None) -> list[int]:
    """Patch the task object the sweep will import, not a dotted path.

    tests/tasks/test_reconcile_vector_index.py evicts ``app.tasks.*`` from
    sys.modules, after which monkeypatch cannot walk the dotted string
    ``app.tasks.url_processing.process_url_request.kiq``. Importing here rebinds
    the module first, so the patch lands on the same object the sweep's own lazy
    import resolves to regardless of run order.
    """
    from app.tasks.url_processing import process_url_request

    kicked: list[int] = []
    failing = fail_on or set()

    async def _kiq(*, request_id: int) -> None:
        if request_id in failing:
            raise RuntimeError("broker unavailable")
        kicked.append(request_id)

    monkeypatch.setattr(process_url_request, "kiq", _kiq)
    return kicked


async def test_due_requests_are_rekicked(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_repos(monkeypatch, retryable=[11, 22])
    kicked = _patch_kiq(monkeypatch)

    assert await _requeue_due_url_requests(object()) == 2
    assert kicked == [11, 22], (
        "the sweep is the only thing that re-drives a Telegram-owned failed job; "
        "without the kick the request stops at its first attempt"
    )


async def test_nothing_due_kicks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_repos(monkeypatch, retryable=[])
    kicked = _patch_kiq(monkeypatch)

    assert await _requeue_due_url_requests(object()) == 0
    assert kicked == []


async def test_one_unkickable_row_does_not_strand_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_repos(monkeypatch, retryable=[11, 22, 33])
    kicked = _patch_kiq(monkeypatch, fail_on={22})

    assert await _requeue_due_url_requests(object()) == 2
    assert kicked == [11, 33]


async def test_a_scan_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken sweep must not take the worker's whole task run down with it."""
    _patch_repos(monkeypatch, retryable=RuntimeError("db down"))
    kicked = _patch_kiq(monkeypatch)

    assert await _requeue_due_url_requests(object()) == 0
    assert kicked == []


async def test_stale_imports_are_failed(monkeypatch: pytest.MonkeyPatch, cfg: Any) -> None:
    calls = _patch_repos(monkeypatch, reaped_imports=[7, 8])

    assert await _fail_stale_imports(cfg, object()) == 2
    assert calls["older_than_seconds"] == 900


async def test_import_scan_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch, cfg: Any) -> None:
    _patch_repos(monkeypatch, reaped_imports=RuntimeError("db down"))

    assert await _fail_stale_imports(cfg, object()) == 0


async def test_body_reports_both_halves(monkeypatch: pytest.MonkeyPatch, cfg: Any) -> None:
    _patch_repos(monkeypatch, retryable=[11], reaped_imports=[7])
    _patch_kiq(monkeypatch)

    stats = await _reap_body(cfg, object())

    assert (stats.requeued_requests, stats.failed_imports) == (1, 1)


async def test_a_failing_url_half_still_runs_the_import_half(
    monkeypatch: pytest.MonkeyPatch, cfg: Any
) -> None:
    """The two halves are independent; one broken table must not mask the other."""
    _patch_repos(monkeypatch, retryable=RuntimeError("db down"), reaped_imports=[7])
    _patch_kiq(monkeypatch)

    stats = await _reap_body(cfg, object())

    assert (stats.requeued_requests, stats.failed_imports) == (0, 1)


async def test_the_sweep_bounds_its_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unbounded sweep would flood the worker queue after a long outage."""
    calls = _patch_repos(monkeypatch, retryable=[])
    _patch_kiq(monkeypatch)

    await _requeue_due_url_requests(object())

    assert calls["limit"] > 0


async def test_a_held_lock_skips_the_sweep(monkeypatch: pytest.MonkeyPatch, cfg: Any) -> None:
    from app.tasks import job_reaper

    class _HeldLock:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> bool:
            return False

        async def __aexit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr(job_reaper, "RedisDistributedLock", _HeldLock)
    monkeypatch.setattr(job_reaper, "get_redis", AsyncMock(return_value=object()))
    body = AsyncMock()
    monkeypatch.setattr(job_reaper, "_reap_body", body)

    stats = await job_reaper.reap_stalled_jobs(cfg=cfg, db=object())

    assert (stats.requeued_requests, stats.failed_imports) == (0, 0)
    body.assert_not_awaited()
