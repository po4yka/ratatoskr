"""Hermetic tests for app.adapters.git_backup.health_ping.

Verifies that:
- ping_start POSTs to {url}/start
- ping_success POSTs to {url}
- ping_failure POSTs to {url}/fail
- ping_failure trims an optional body
- A transport that raises an exception is swallowed (no re-raise)

Uses httpx.MockTransport injected via unittest.mock.patch so no network is
required and the test is fully hermetic.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.adapters.git_backup.health_ping import ping_failure, ping_start, ping_success

_BASE_URL = "https://hc-ping.com/test-uuid"
_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_transport(handler=None):
    """Return an httpx.MockTransport that records the last request."""
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        if handler:
            return handler(request)
        return httpx.Response(200)

    return httpx.MockTransport(_handler), captured


def _patch_client(transport: httpx.MockTransport):
    """Replace the safe-client factory with an isolated mock transport."""
    return patch(
        "app.adapters.git_backup.health_ping.make_safe_async_client",
        return_value=httpx.AsyncClient(transport=transport),
    )


# ---------------------------------------------------------------------------
# ping_start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_start_posts_to_start_url() -> None:
    transport, captured = _make_transport()
    with _patch_client(transport):
        await ping_start(_BASE_URL, _TIMEOUT)
    assert captured["request"].url == httpx.URL(f"{_BASE_URL}/start")
    assert captured["request"].method == "POST"


# ---------------------------------------------------------------------------
# ping_success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_success_posts_to_base_url() -> None:
    transport, captured = _make_transport()
    with _patch_client(transport):
        await ping_success(_BASE_URL, _TIMEOUT)
    assert captured["request"].url == httpx.URL(_BASE_URL)
    assert captured["request"].method == "POST"


# ---------------------------------------------------------------------------
# ping_failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_failure_posts_to_fail_url() -> None:
    transport, captured = _make_transport()
    with _patch_client(transport):
        await ping_failure(_BASE_URL, _TIMEOUT)
    assert captured["request"].url == httpx.URL(f"{_BASE_URL}/fail")
    assert captured["request"].method == "POST"


@pytest.mark.asyncio
async def test_ping_failure_sends_body_when_provided() -> None:
    transport, captured = _make_transport()
    with _patch_client(transport):
        await ping_failure(_BASE_URL, _TIMEOUT, body="something went wrong")
    assert b"something went wrong" in captured["request"].content


@pytest.mark.asyncio
async def test_ping_failure_trims_body_to_10000_bytes() -> None:
    long_body = "x" * 20_000
    transport, captured = _make_transport()
    with _patch_client(transport):
        await ping_failure(_BASE_URL, _TIMEOUT, body=long_body)
    assert len(captured["request"].content) <= 10_000


@pytest.mark.asyncio
async def test_ping_failure_no_body_sends_empty_content() -> None:
    transport, captured = _make_transport()
    with _patch_client(transport):
        await ping_failure(_BASE_URL, _TIMEOUT, body=None)
    assert captured["request"].content == b""


@pytest.mark.asyncio
async def test_ping_failure_log_does_not_expose_secret_url(caplog) -> None:
    secret_url = "https://hc-ping.com/secret-check-uuid"

    def _raise(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed to connect to {secret_url}")

    transport, _captured = _make_transport(_raise)
    with _patch_client(transport), caplog.at_level("WARNING"):
        await ping_success(secret_url, _TIMEOUT)

    assert "git_backup_health_ping_failed" in caplog.text
    assert "secret-check-uuid" not in caplog.text
    assert caplog.records[-1].error_type == "ConnectError"
