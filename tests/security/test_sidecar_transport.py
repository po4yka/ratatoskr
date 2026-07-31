"""Sidecar connections must not go through the SSRF transport.

SafeAsyncTransport validates the resolved IP against BLOCKED_NETWORKS, which
covers every RFC1918 range -- exactly where a compose sidecar lives. Pointing it
at our own sidecar made the connection fail before it was attempted, so the
Defuddle rung was dead in every containerised deployment (enabled by default,
failure logged at debug) while /health still advertised it.
"""

from __future__ import annotations

import ipaddress

import pytest

from app.adapters.content.scraper import (
    crawl4ai_provider,
    defuddle_provider,
    webwright_provider,
)
from app.adapters.webwright import client as webwright_client
from app.security import ssrf


def test_compose_sidecar_addresses_are_blocked_by_the_safe_transport() -> None:
    """The premise: this is why a safe client cannot reach a sidecar."""
    for addr in ("172.18.0.5", "10.0.0.7", "192.168.1.10"):
        assert any(ipaddress.ip_address(addr) in net for net in ssrf.BLOCKED_NETWORKS), (
            f"{addr} unexpectedly allowed"
        )


def test_trusted_client_has_no_ssrf_transport() -> None:
    client = ssrf.make_trusted_sidecar_client()
    transport = client._transport
    assert not isinstance(transport, ssrf.SafeAsyncTransport)


def test_safe_client_still_has_the_ssrf_transport() -> None:
    """The user-facing factory must be unchanged."""
    client = ssrf.make_safe_async_client()
    assert isinstance(client._transport, ssrf.SafeAsyncTransport)


@pytest.mark.parametrize(
    "module",
    [defuddle_provider, webwright_provider, webwright_client, crawl4ai_provider],
    ids=["defuddle", "webwright_provider", "webwright_client", "crawl4ai"],
)
def test_sidecar_modules_use_the_trusted_factory(module: object) -> None:
    """Each of these talks only to an operator-configured host."""
    assert hasattr(module, "make_trusted_sidecar_client")
    assert not hasattr(module, "make_safe_async_client"), (
        "a sidecar module still imports the SSRF-transport factory"
    )


def test_defuddle_still_validates_the_caller_url() -> None:
    """Bypassing the transport must not drop the user-URL check."""
    import inspect

    source = inspect.getsource(defuddle_provider.DefuddleProvider._fetch_raw)
    assert "is_url_safe_async(url)" in source
    assert "SSRF blocked redirect target" in source
