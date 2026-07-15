"""Concurrency and cancellation contracts for auth Argon2 offload."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.api.exceptions import AuthenticationError, ResourceNotFoundError
from app.api.models.auth import CredentialsLoginRequest, SecretLoginRequest
from app.api.routers.auth import argon2_offload, endpoints_credentials, endpoints_secret_keys
from app.config.api import AuthConfig


@pytest.fixture(autouse=True)
def _reset_offloader() -> None:
    argon2_offload._reset_for_tests()
    yield
    argon2_offload._reset_for_tests()


async def _wait_for_thread_event(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 2)


async def test_run_argon2_executes_outside_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(argon2_offload, "_get_max_concurrency", lambda: 1)
    event_loop_thread = threading.get_ident()

    worker_thread = await argon2_offload.run_argon2(threading.get_ident)

    assert worker_thread != event_loop_thread


async def test_credentials_login_decoy_is_offloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(argon2_offload, "_get_max_concurrency", lambda: 1)
    monkeypatch.setattr(endpoints_credentials, "validate_client_id", lambda _value: None)
    monkeypatch.setattr(endpoints_credentials, "validate_password", lambda _value: None)
    monkeypatch.setattr(
        endpoints_credentials,
        "canonicalize_identifier",
        lambda _value: ("nickname", "missing", "missing"),
    )
    repository = MagicMock()
    repository.async_get_by_canonical = AsyncMock(return_value=None)
    monkeypatch.setattr(
        endpoints_credentials,
        "get_user_credential_repository",
        lambda: repository,
    )
    worker_threads: list[int] = []
    monkeypatch.setattr(
        endpoints_credentials,
        "run_decoy_verify",
        lambda _password: worker_threads.append(threading.get_ident()),
    )
    event_loop_thread = threading.get_ident()

    with pytest.raises(AuthenticationError):
        await endpoints_credentials.credentials_login(
            CredentialsLoginRequest(
                identifier="missing",
                password="correct horse battery staple",
                remember_me=False,
                client_id="web-v1",
            ),
            MagicMock(),
        )

    assert worker_threads and worker_threads[0] != event_loop_thread


async def test_secret_login_decoy_is_offloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(argon2_offload, "_get_max_concurrency", lambda: 1)
    monkeypatch.setattr(endpoints_secret_keys, "ensure_secret_login_enabled", lambda: None)
    monkeypatch.setattr(endpoints_secret_keys, "validate_client_id", lambda _value: None)
    monkeypatch.setattr(endpoints_secret_keys, "ensure_user_allowed", lambda _value: None)
    repository = MagicMock()
    repository.async_get_user_by_telegram_id = AsyncMock(return_value=None)
    monkeypatch.setattr(endpoints_secret_keys, "get_user_repository", lambda: repository)
    worker_threads: list[int] = []
    monkeypatch.setattr(
        endpoints_secret_keys,
        "run_decoy_secret_verify",
        lambda _secret: worker_threads.append(threading.get_ident()),
    )
    event_loop_thread = threading.get_ident()

    with pytest.raises(ResourceNotFoundError):
        await endpoints_secret_keys.secret_login(
            SecretLoginRequest(
                user_id=123,
                client_id="mobile-client",
                secret="long-enough-client-secret",
                username="owner",
            ),
            MagicMock(),
        )

    assert worker_threads and worker_threads[0] != event_loop_thread


async def test_run_argon2_bounds_active_and_queued_executor_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(argon2_offload, "_get_max_concurrency", lambda: 2)
    release = threading.Event()
    two_started = threading.Event()
    state_lock = threading.Lock()
    active = 0
    max_active = 0
    started = 0

    def memory_hard_work() -> None:
        nonlocal active, max_active, started
        with state_lock:
            active += 1
            started += 1
            max_active = max(max_active, active)
            if started == 2:
                two_started.set()
        release.wait(timeout=2)
        with state_lock:
            active -= 1

    tasks = [asyncio.create_task(argon2_offload.run_argon2(memory_hard_work)) for _ in range(5)]
    await _wait_for_thread_event(two_started)

    # The other calls are waiting on the admission semaphore and have not
    # entered ThreadPoolExecutor's unbounded internal work queue.
    assert started == 2
    assert max_active == 2

    release.set()
    await asyncio.gather(*tasks)
    assert max_active == 2


async def test_cancellation_keeps_slot_until_worker_releases_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(argon2_offload, "_get_max_concurrency", lambda: 1)
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()

    def first_work() -> None:
        first_started.set()
        release_first.wait(timeout=2)

    def second_work() -> None:
        second_started.set()

    first = asyncio.create_task(argon2_offload.run_argon2(first_work))
    await _wait_for_thread_event(first_started)
    first.cancel()
    second = asyncio.create_task(argon2_offload.run_argon2(second_work))

    await asyncio.sleep(0.05)
    assert not first.done()
    assert not second_started.is_set()

    release_first.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert second_started.is_set()


def test_auth_config_bounds_argon2_concurrency() -> None:
    assert AuthConfig().argon2_max_concurrency == 2
    assert AuthConfig(argon2_max_concurrency="8").argon2_max_concurrency == 8

    for invalid in (0, 9, "not-an-integer"):
        with pytest.raises(ValidationError):
            AuthConfig(argon2_max_concurrency=invalid)
