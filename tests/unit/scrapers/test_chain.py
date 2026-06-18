"""Unit tests for ``ContentScraperChain``.

Covers the explicit goal criteria:

- Scraper chain order (providers tried in the order they were registered).
- Scraper fallthrough when one provider raises an exception.
- Scraper fallthrough when one provider returns an error status / empty body /
  error-page text / sub-threshold content.
- Aggregate-error path when every provider fails.

Every external collaborator is replaced with an in-memory stub so the tests
are network-free and key-free.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.adapters.content.scraper.chain import ContentScraperChain
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus

pytestmark = pytest.mark.no_network


@pytest.fixture(autouse=True)
def _allow_safe_scraper_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _safe(_url: str) -> tuple[bool, None]:
        return (True, None)

    monkeypatch.setattr(
        "app.adapters.content.scraper.chain.is_url_safe_async",
        _safe,
    )


class _FakeProvider:
    """Tiny in-memory ``ContentScraperProtocol`` double.

    `behaviour` is either a ``FirecrawlResult`` to return or an Exception to raise.
    """

    def __init__(self, name: str, behaviour: Any) -> None:
        self._name = name
        self._behaviour = behaviour
        self.calls: list[tuple[str, bool, int | None]] = []
        self.closed = False

    @property
    def provider_name(self) -> str:
        return self._name

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        self.calls.append((url, mobile, request_id))
        if isinstance(self._behaviour, BaseException):
            raise self._behaviour
        return self._behaviour

    async def aclose(self) -> None:
        self.closed = True


def _ok_result(text: str = "Article body that is plenty long.") -> FirecrawlResult:
    return FirecrawlResult(
        status=CallStatus.OK,
        content_markdown=text,
        source_url="https://example.com/article",
        latency_ms=10,
    )


def _error_result(message: str = "boom") -> FirecrawlResult:
    return FirecrawlResult(
        status=CallStatus.ERROR,
        error_text=message,
        source_url="https://example.com/article",
    )


def test_chain_constructor_rejects_empty_provider_list() -> None:
    with pytest.raises(ValueError, match="at least one provider"):
        ContentScraperChain(providers=[])


def test_chain_providers_property_returns_a_copy() -> None:
    p1 = _FakeProvider("a", _ok_result())
    chain = ContentScraperChain(providers=[p1])

    view = chain.providers
    view.clear()  # mutating the view must not affect internal state
    assert [p.provider_name for p in chain.providers] == ["a"]


def test_chain_provider_name_is_chain() -> None:
    chain = ContentScraperChain(providers=[_FakeProvider("a", _ok_result())])
    assert chain.provider_name == "chain"


@pytest.mark.asyncio
async def test_chain_returns_first_provider_success_and_skips_later_ones() -> None:
    """Order matters: first OK result short-circuits the rest."""
    first = _FakeProvider("primary", _ok_result("primary content"))
    second = _FakeProvider("secondary", _ok_result("secondary content"))

    chain = ContentScraperChain(providers=[first, second])
    result = await chain.scrape_markdown("https://example.com/a")

    assert result.status == CallStatus.OK
    assert result.content_markdown == "primary content"
    assert len(first.calls) == 1
    assert second.calls == []


@pytest.mark.asyncio
async def test_chain_falls_through_when_provider_raises() -> None:
    """An exception from a provider must not bubble; the chain moves on."""
    boom = _FakeProvider("boom", RuntimeError("connection reset"))
    survivor = _FakeProvider("survivor", _ok_result("recovered"))

    chain = ContentScraperChain(providers=[boom, survivor])
    result = await chain.scrape_markdown("https://example.com/b")

    assert result.status == CallStatus.OK
    assert result.content_markdown == "recovered"
    assert len(boom.calls) == 1
    assert len(survivor.calls) == 1


@pytest.mark.asyncio
async def test_chain_falls_through_on_error_status() -> None:
    """A provider returning CallStatus.ERROR yields control to the next provider."""
    bad = _FakeProvider("bad", _error_result("upstream 500"))
    good = _FakeProvider("good", _ok_result())

    chain = ContentScraperChain(providers=[bad, good])
    result = await chain.scrape_markdown("https://example.com/c")

    assert result.status == CallStatus.OK
    assert len(bad.calls) == 1 and len(good.calls) == 1


@pytest.mark.asyncio
async def test_chain_falls_through_on_empty_content() -> None:
    empty = _FakeProvider(
        "empty",
        FirecrawlResult(status=CallStatus.OK, content_markdown="   ", source_url="x"),
    )
    good = _FakeProvider("good", _ok_result("real body"))

    chain = ContentScraperChain(providers=[empty, good])
    result = await chain.scrape_markdown("https://example.com/d")

    assert result.status == CallStatus.OK
    assert result.content_markdown == "real body"


@pytest.mark.asyncio
async def test_chain_skips_error_page_content() -> None:
    """Short content that matches an error-page pattern is rejected."""
    error_page = _FakeProvider(
        "error_page",
        FirecrawlResult(
            status=CallStatus.OK,
            content_markdown="404 not found",
            source_url="x",
        ),
    )
    good = _FakeProvider("good", _ok_result())

    chain = ContentScraperChain(providers=[error_page, good])
    result = await chain.scrape_markdown("https://example.com/e")

    assert result.status == CallStatus.OK
    assert result.content_markdown == "Article body that is plenty long."


@pytest.mark.asyncio
async def test_chain_respects_min_content_length() -> None:
    short = _FakeProvider(
        "short",
        FirecrawlResult(status=CallStatus.OK, content_markdown="tiny", source_url="x"),
    )
    # Use varied content so the low-value-content quality filter doesn't reject it.
    rich_text = " ".join(f"sentence number {i} about widgets and gears." for i in range(120))
    longer = _FakeProvider(
        "longer",
        FirecrawlResult(
            status=CallStatus.OK,
            content_markdown=rich_text,
            source_url="x",
        ),
    )

    chain = ContentScraperChain(
        providers=[short, longer],
        min_content_length=200,
    )
    result = await chain.scrape_markdown("https://example.com/f")
    assert result.status == CallStatus.OK
    assert result.content_markdown == rich_text


@pytest.mark.asyncio
async def test_chain_returns_aggregated_error_when_all_providers_fail() -> None:
    a = _FakeProvider("a", _error_result("e_a"))
    b = _FakeProvider("b", RuntimeError("e_b"))
    c = _FakeProvider("c", _error_result("e_c"))

    chain = ContentScraperChain(providers=[a, b, c])
    result = await chain.scrape_markdown("https://example.com/g")

    assert result.status == CallStatus.ERROR
    assert result.error_text is not None
    assert "All providers failed" in result.error_text
    assert "e_a" in result.error_text and "e_b" in result.error_text and "e_c" in result.error_text
    # Every provider must have been tried even though all failed.
    assert len(a.calls) == 1 and len(b.calls) == 1 and len(c.calls) == 1


@pytest.mark.asyncio
async def test_chain_stamps_attempt_log_on_exhaustion() -> None:
    """Failure path must populate attempt_log so DB triage doesn't need log greps."""
    a = _FakeProvider("a", _error_result("upstream 500"))
    b = _FakeProvider("b", RuntimeError("boom"))

    chain = ContentScraperChain(providers=[a, b])
    result = await chain.scrape_markdown("https://example.com/exhausted")

    assert result.status == CallStatus.ERROR
    options = result.options_json or {}
    attempt_log = options["_chain_attempt_log"]
    providers_recorded = [e["provider"] for e in attempt_log]
    assert providers_recorded == ["a", "b"]
    assert all(e["status"] == "error" for e in attempt_log)
    assert attempt_log[1]["error_class"] == "RuntimeError"
    assert attempt_log[1]["error_message"] == "boom"
    assert attempt_log[1]["bytes_extracted"] == 0
    assert options["_chain_winning_provider"] is None


@pytest.mark.asyncio
async def test_chain_stamps_attempt_log_on_success() -> None:
    """Success path records winner provider so persisted row carries the verdict."""
    a = _FakeProvider("a", _error_result("upstream 500"))
    b = _FakeProvider("b", _ok_result("recovered"))

    chain = ContentScraperChain(providers=[a, b])
    result = await chain.scrape_markdown("https://example.com/recovered")

    assert result.status == CallStatus.OK
    options = result.options_json or {}
    attempt_log = options["_chain_attempt_log"]
    assert [e["provider"] for e in attempt_log] == ["a", "b"]
    assert [e["status"] for e in attempt_log] == ["error", "success"]
    assert attempt_log[0]["error_message"] == "upstream 500"
    assert attempt_log[1]["bytes_extracted"] == len("recovered")
    assert options["_chain_winning_provider"] == "b"


@pytest.mark.asyncio
async def test_chain_records_two_failures_then_success_with_winning_provider() -> None:
    """Persisted attempt log shape covers multi-provider recovery."""
    first = _FakeProvider("scrapling", _error_result("upstream 500"))
    second = _FakeProvider("firecrawl", TimeoutError("timed out"))
    third = _FakeProvider("direct_html", _ok_result("recovered article"))

    chain = ContentScraperChain(providers=[first, second, third], race_enabled=False)
    result = await chain.scrape_markdown("https://example.com/recovered-late")

    assert result.status == CallStatus.OK
    options = result.options_json or {}
    assert options["_chain_winning_provider"] == "direct_html"
    assert options["_chain_attempt_log"] == [
        {
            "provider": "scrapling",
            "status": "error",
            "latency_ms": options["_chain_attempt_log"][0]["latency_ms"],
            "error_class": "no_content",
            "error_message": "upstream 500",
            "bytes_extracted": 0,
        },
        {
            "provider": "firecrawl",
            "status": "timeout",
            "latency_ms": options["_chain_attempt_log"][1]["latency_ms"],
            "error_class": "TimeoutError",
            "error_message": "timed out",
            "bytes_extracted": 0,
        },
        {
            "provider": "direct_html",
            "status": "success",
            "latency_ms": options["_chain_attempt_log"][2]["latency_ms"],
            "error_class": None,
            "error_message": None,
            "bytes_extracted": len("recovered article"),
        },
    ]


@pytest.mark.asyncio
async def test_chain_blocks_unsafe_url_before_provider_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider("primary", _ok_result("should not be called"))

    async def _unsafe(_url: str) -> tuple[bool, str]:
        return (False, "Private or reserved IP address: 127.0.0.1")

    monkeypatch.setattr(
        "app.adapters.content.scraper.chain.is_url_safe_async",
        _unsafe,
    )

    chain = ContentScraperChain(providers=[provider])
    result = await chain.scrape_markdown("http://127.0.0.1/admin")

    assert result.status == CallStatus.ERROR
    assert "SSRF blocked URL" in (result.error_text or "")
    assert result.options_json == {"_chain_attempt_log": [], "_chain_winning_provider": None}
    assert provider.calls == []


@pytest.mark.asyncio
async def test_chain_invokes_audit_callback_on_success() -> None:
    received: list[tuple[str, str, dict]] = []

    def audit(level: str, event: str, payload: dict) -> None:
        received.append((level, event, payload))

    chain = ContentScraperChain(
        providers=[_FakeProvider("p", _ok_result())],
        audit=audit,
    )
    await chain.scrape_markdown("https://example.com/h")

    assert any(event == "scraper_chain_success" for _, event, _ in received)


@pytest.mark.asyncio
async def test_chain_reorders_browser_providers_for_js_heavy_urls() -> None:
    """When the URL host matches a js_heavy_host, browser providers move to the front."""
    # ``playwright`` is in BROWSER_PROVIDERS; ``direct_html`` is not.
    text_provider = _FakeProvider("direct_html", _ok_result("text-rendered"))
    browser_provider = _FakeProvider("playwright", _ok_result("js-rendered"))

    chain = ContentScraperChain(
        providers=[text_provider, browser_provider],
        js_heavy_hosts=("example.com",),
    )
    result = await chain.scrape_markdown("https://example.com/spa")
    # Browser provider should have been tried first.
    assert result.content_markdown == "js-rendered"
    assert len(browser_provider.calls) == 1
    assert text_provider.calls == []


@pytest.mark.asyncio
async def test_chain_does_not_reorder_for_non_js_heavy_url() -> None:
    text_provider = _FakeProvider("direct_html", _ok_result("text-rendered"))
    browser_provider = _FakeProvider("playwright", _ok_result("js-rendered"))

    chain = ContentScraperChain(
        providers=[text_provider, browser_provider],
        js_heavy_hosts=("nope.example.com",),
    )
    result = await chain.scrape_markdown("https://example.com/plain")
    assert result.content_markdown == "text-rendered"


@pytest.mark.asyncio
async def test_chain_aclose_closes_every_provider() -> None:
    a = _FakeProvider("a", _ok_result())
    b = _FakeProvider("b", _ok_result())
    chain = ContentScraperChain(providers=[a, b])

    await chain.aclose()
    assert a.closed is True and b.closed is True


@pytest.mark.asyncio
async def test_chain_aclose_swallows_provider_exceptions() -> None:
    class _Bad(_FakeProvider):
        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    bad = _Bad("bad", _ok_result())
    good = _FakeProvider("good", _ok_result())

    chain = ContentScraperChain(providers=[bad, good])
    # Must not raise even if a provider's aclose() does.
    await chain.aclose()
    assert good.closed is True


@pytest.mark.asyncio
async def test_chain_forwards_mobile_and_request_id_to_providers() -> None:
    provider = _FakeProvider("p", _ok_result())
    chain = ContentScraperChain(providers=[provider])

    await chain.scrape_markdown(
        "https://example.com/i",
        mobile=False,
        request_id=4242,
    )

    assert provider.calls == [("https://example.com/i", False, 4242)]


@pytest.mark.asyncio
async def test_chain_treats_html_only_result_as_content() -> None:
    """Result with HTML but no markdown should still be accepted as content."""
    provider = _FakeProvider(
        "html_only",
        FirecrawlResult(
            status=CallStatus.OK,
            content_html="<article>" + "x" * 5000 + "</article>",
            source_url="https://example.com/html",
        ),
    )
    chain = ContentScraperChain(providers=[provider])

    result = await chain.scrape_markdown("https://example.com/j")
    assert result.status == CallStatus.OK
    assert result.content_html is not None


@pytest.mark.asyncio
async def test_chain_uses_async_mock_provider_via_unittest_mock() -> None:
    """Sanity check that an unittest.mock-based provider also works."""
    mock_provider = AsyncMock()
    mock_provider.provider_name = "mocked"
    mock_provider.scrape_markdown = AsyncMock(return_value=_ok_result("ok-mock"))
    mock_provider.aclose = AsyncMock()

    chain = ContentScraperChain(providers=[mock_provider])
    result = await chain.scrape_markdown("https://example.com/k")
    assert result.content_markdown == "ok-mock"
    mock_provider.scrape_markdown.assert_awaited_once()
