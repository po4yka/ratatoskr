"""Regression guard: SSRF checks on async paths must not resolve DNS on the event loop.

``socket.getaddrinfo`` blocks for the full OS resolver timeout when a hostname
points at a slow or blackholed resolver. Called straight from an ``async def``
API handler it freezes the whole process -- every other request, every health
check -- which one authenticated call can trigger repeatedly.

Each test below poisons the blocking resolver (:func:`resolve_host_ips`) and
stubs the non-blocking one, so any async path that regresses to the sync
``is_url_safe`` fails loudly instead of silently reintroducing the stall.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.models.requests import CreateAggregationBundleRequest
from app.api.routers.content.aggregation import _ensure_public_bundle_urls
from app.domain.services.rule_engine import validate_rule
from app.domain.services.webhook_service import is_webhook_url_safe, validate_webhook_url

_PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _poison_blocking_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the blocking resolver fail, and the async one return a public IP."""

    def _blocking(_hostname: str) -> list[str]:
        raise AssertionError("blocking DNS resolution reached from an async path")

    async def _non_blocking(_hostname: str, **_kwargs: Any) -> list[str]:
        return [_PUBLIC_IP]

    monkeypatch.setattr("app.security.ssrf.resolve_host_ips", _blocking)
    monkeypatch.setattr("app.security.ssrf.resolve_host_ips_async", _non_blocking)


async def test_is_webhook_url_safe_stays_off_the_loop() -> None:
    safe, reason = await is_webhook_url_safe("https://example.com/hook")
    assert safe is True
    assert reason is None


async def test_validate_webhook_url_stays_off_the_loop() -> None:
    valid, error = await validate_webhook_url("https://example.com/hook")
    assert valid is True
    assert error is None


async def test_validate_rule_send_webhook_stays_off_the_loop() -> None:
    valid, error = await validate_rule(
        "summary.created",
        [],
        [{"type": "send_webhook", "params": {"url": "https://example.com/hook"}}],
        "all",
    )
    assert valid is True
    assert error is None


async def test_aggregation_bundle_check_stays_off_the_loop() -> None:
    body = CreateAggregationBundleRequest(items=[{"url": "https://example.com/article"}])

    await _ensure_public_bundle_urls(
        body=body,
        audit=lambda *_args, **_kwargs: None,
        audit_context={},
    )
