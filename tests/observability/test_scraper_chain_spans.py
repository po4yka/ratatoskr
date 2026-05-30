"""Unit tests for scraper-chain OTel span attribute enrichment.

Covers Phase 1 telemetry wiring:
  - Per-rung scraper.<name> span: SCRAPER_PROVIDER, SCRAPER_OUTCOME,
    SCRAPER_TIER, SCRAPER_REQUEST_ID, SCRAPER_CONTENT_LEN, SCRAPER_TIMEOUT_SEC
  - Parent scraper.chain span: SCRAPER_WINNER, SCRAPER_ATTEMPTS,
    SCRAPER_CONTENT_LEN (on winning provider)
  - record_scraper_attempt / record_scraper_attempt_latency called at each outcome

All OTel spans are captured via an in-process ``_RecordingTracer`` so the
tests are network-free and require no OTLP endpoint.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from app.adapters.content.scraper.chain import ContentScraperChain
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.observability.attributes import (
    SCRAPER_ATTEMPTS,
    SCRAPER_CONTENT_LEN,
    SCRAPER_OUTCOME,
    SCRAPER_PROVIDER,
    SCRAPER_REQUEST_ID,
    SCRAPER_TIER,
    SCRAPER_WINNER,
)

pytestmark = pytest.mark.no_network


# ---------------------------------------------------------------------------
# OTel recording helpers
# ---------------------------------------------------------------------------


class _RecordingSpan:
    """Minimal span double that stores set_attribute calls."""

    def __init__(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.name = name
        self.attrs: dict[str, Any] = dict(attributes or {})

    def set_attribute(self, key: str, value: Any) -> None:
        self.attrs[key] = value

    def record_exception(self, exc: BaseException, **_: Any) -> None:
        pass

    def set_status(self, *_: Any, **__: Any) -> None:
        pass

    def is_recording(self) -> bool:
        return True

    def __enter__(self) -> _RecordingSpan:
        return self

    def __exit__(self, *_: Any) -> None:
        pass


class _RecordingTracer:
    """Minimal tracer double that records every span opened."""

    def __init__(self) -> None:
        self.spans: list[_RecordingSpan] = []

    def start_as_current_span(
        self, name: str, attributes: dict[str, Any] | None = None, **_: Any
    ) -> _RecordingSpan:
        span = _RecordingSpan(name, attributes)
        self.spans.append(span)
        return span

    def span_by_name(self, name: str) -> _RecordingSpan | None:
        for s in self.spans:
            if s.name == name:
                return s
        return None

    def spans_by_prefix(self, prefix: str) -> list[_RecordingSpan]:
        return [s for s in self.spans if s.name.startswith(prefix)]


# ---------------------------------------------------------------------------
# Provider doubles
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, name: str, behaviour: Any, *, timeout_sec: float | None = None) -> None:
        self._name = name
        self._behaviour = behaviour
        self.timeout_sec = timeout_sec

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
        if isinstance(self._behaviour, BaseException):
            raise self._behaviour
        return self._behaviour

    async def aclose(self) -> None:
        pass


def _ok_result(text: str = "Article body that is plenty long.") -> FirecrawlResult:
    return FirecrawlResult(
        status=CallStatus.OK,
        content_markdown=text,
        source_url="https://example.com/article",
        latency_ms=10,
    )


def _error_result(message: str = "upstream 500") -> FirecrawlResult:
    return FirecrawlResult(
        status=CallStatus.ERROR,
        error_text=message,
        source_url="https://example.com/article",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _allow_safe_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.adapters.content.scraper.chain.is_url_safe",
        lambda _url: (True, None),
    )


@pytest.fixture
def recording_tracer() -> _RecordingTracer:
    return _RecordingTracer()


# ---------------------------------------------------------------------------
# Helper that runs the chain with the recording tracer injected
# ---------------------------------------------------------------------------


async def _run_chain(
    chain: ContentScraperChain,
    tracer: _RecordingTracer,
    url: str = "https://example.com/article",
    request_id: int | None = None,
) -> FirecrawlResult:
    # get_tracer is imported locally inside scrape_markdown, so patch it at the
    # otel module level (the call site resolves it from there at import time).
    with patch("app.observability.otel.get_tracer", return_value=tracer):
        return await chain.scrape_markdown(url, request_id=request_id)


# ---------------------------------------------------------------------------
# Per-rung span: core attribute wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_span_has_scraper_provider_attribute(
    recording_tracer: _RecordingTracer,
) -> None:
    provider = _FakeProvider("scrapling", _ok_result())
    chain = ContentScraperChain(providers=[provider], race_enabled=False)
    await _run_chain(chain, recording_tracer)

    rung = recording_tracer.span_by_name("scraper.scrapling")
    assert rung is not None
    assert rung.attrs.get(SCRAPER_PROVIDER) == "scrapling"


@pytest.mark.asyncio
async def test_provider_span_outcome_success(recording_tracer: _RecordingTracer) -> None:
    provider = _FakeProvider("scrapling", _ok_result())
    chain = ContentScraperChain(providers=[provider], race_enabled=False)
    await _run_chain(chain, recording_tracer)

    rung = recording_tracer.span_by_name("scraper.scrapling")
    assert rung is not None
    assert rung.attrs.get(SCRAPER_OUTCOME) == "success"


@pytest.mark.asyncio
async def test_provider_span_outcome_no_content(recording_tracer: _RecordingTracer) -> None:
    provider = _FakeProvider("scrapling", _error_result())
    fallback = _FakeProvider("crawl4ai", _ok_result())
    chain = ContentScraperChain(providers=[provider, fallback], race_enabled=False)
    await _run_chain(chain, recording_tracer)

    rung = recording_tracer.span_by_name("scraper.scrapling")
    assert rung is not None
    assert rung.attrs.get(SCRAPER_OUTCOME) == "no_content"


@pytest.mark.asyncio
async def test_provider_span_outcome_error_on_exception(recording_tracer: _RecordingTracer) -> None:
    provider = _FakeProvider("scrapling", RuntimeError("connection reset"))
    fallback = _FakeProvider("crawl4ai", _ok_result())
    chain = ContentScraperChain(providers=[provider, fallback], race_enabled=False)
    await _run_chain(chain, recording_tracer)

    rung = recording_tracer.span_by_name("scraper.scrapling")
    assert rung is not None
    assert rung.attrs.get(SCRAPER_OUTCOME) == "error"


@pytest.mark.asyncio
async def test_provider_span_has_tier_attribute(recording_tracer: _RecordingTracer) -> None:
    """SCRAPER_TIER is set on the per-rung span regardless of outcome."""
    provider = _FakeProvider("scrapling", _ok_result())
    chain = ContentScraperChain(providers=[provider], race_enabled=False)
    await _run_chain(chain, recording_tracer)

    rung = recording_tracer.span_by_name("scraper.scrapling")
    assert rung is not None
    assert SCRAPER_TIER in rung.attrs
    # serial path uses tier_index=0 (the single default tier)
    assert isinstance(rung.attrs[SCRAPER_TIER], int)


@pytest.mark.asyncio
async def test_provider_span_has_request_id_when_provided(
    recording_tracer: _RecordingTracer,
) -> None:
    provider = _FakeProvider("scrapling", _ok_result())
    chain = ContentScraperChain(providers=[provider], race_enabled=False)
    await _run_chain(chain, recording_tracer, request_id=9999)

    rung = recording_tracer.span_by_name("scraper.scrapling")
    assert rung is not None
    assert rung.attrs.get(SCRAPER_REQUEST_ID) == "9999"


@pytest.mark.asyncio
async def test_provider_span_omits_request_id_when_none(
    recording_tracer: _RecordingTracer,
) -> None:
    provider = _FakeProvider("scrapling", _ok_result())
    chain = ContentScraperChain(providers=[provider], race_enabled=False)
    await _run_chain(chain, recording_tracer, request_id=None)

    rung = recording_tracer.span_by_name("scraper.scrapling")
    assert rung is not None
    assert SCRAPER_REQUEST_ID not in rung.attrs


@pytest.mark.asyncio
async def test_provider_span_has_content_len_on_success(
    recording_tracer: _RecordingTracer,
) -> None:
    text = "A" * 500
    provider = _FakeProvider("scrapling", _ok_result(text))
    chain = ContentScraperChain(providers=[provider], race_enabled=False)
    await _run_chain(chain, recording_tracer)

    rung = recording_tracer.span_by_name("scraper.scrapling")
    assert rung is not None
    assert rung.attrs.get(SCRAPER_CONTENT_LEN) == len(text)


@pytest.mark.asyncio
async def test_provider_span_no_content_len_on_error(
    recording_tracer: _RecordingTracer,
) -> None:
    """SCRAPER_CONTENT_LEN must not be set on a failing rung."""
    provider = _FakeProvider("scrapling", _error_result())
    fallback = _FakeProvider("crawl4ai", _ok_result())
    chain = ContentScraperChain(providers=[provider, fallback], race_enabled=False)
    await _run_chain(chain, recording_tracer)

    rung = recording_tracer.span_by_name("scraper.scrapling")
    assert rung is not None
    assert SCRAPER_CONTENT_LEN not in rung.attrs


@pytest.mark.asyncio
async def test_provider_timeout_sec_set_when_provider_exposes_it(
    recording_tracer: _RecordingTracer,
) -> None:
    from app.observability.attributes import SCRAPER_TIMEOUT_SEC

    provider = _FakeProvider("scrapling", _ok_result(), timeout_sec=30.0)
    chain = ContentScraperChain(providers=[provider], race_enabled=False)
    await _run_chain(chain, recording_tracer)

    rung = recording_tracer.span_by_name("scraper.scrapling")
    assert rung is not None
    assert rung.attrs.get(SCRAPER_TIMEOUT_SEC) == 30.0


@pytest.mark.asyncio
async def test_provider_timeout_sec_absent_when_not_on_provider(
    recording_tracer: _RecordingTracer,
) -> None:
    from app.observability.attributes import SCRAPER_TIMEOUT_SEC

    # _FakeProvider without timeout_sec set — attribute should not appear
    provider = _FakeProvider("scrapling", _ok_result())
    provider.timeout_sec = None  # explicitly None
    chain = ContentScraperChain(providers=[provider], race_enabled=False)
    await _run_chain(chain, recording_tracer)

    rung = recording_tracer.span_by_name("scraper.scrapling")
    assert rung is not None
    assert SCRAPER_TIMEOUT_SEC not in rung.attrs


# ---------------------------------------------------------------------------
# Parent chain span: SCRAPER_WINNER, SCRAPER_ATTEMPTS, SCRAPER_CONTENT_LEN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_span_winner_set_on_success(recording_tracer: _RecordingTracer) -> None:
    provider = _FakeProvider("crawl4ai", _ok_result())
    chain = ContentScraperChain(providers=[provider], race_enabled=False)
    await _run_chain(chain, recording_tracer)

    chain_span = recording_tracer.span_by_name("scraper.chain")
    assert chain_span is not None
    assert chain_span.attrs.get(SCRAPER_WINNER) == "crawl4ai"


@pytest.mark.asyncio
async def test_chain_span_attempts_set_to_total_tried(recording_tracer: _RecordingTracer) -> None:
    bad = _FakeProvider("scrapling", _error_result())
    good = _FakeProvider("crawl4ai", _ok_result())
    chain = ContentScraperChain(providers=[bad, good], race_enabled=False)
    await _run_chain(chain, recording_tracer)

    chain_span = recording_tracer.span_by_name("scraper.chain")
    assert chain_span is not None
    # 1 error (scrapling) + winning attempt = 2 total
    assert chain_span.attrs.get(SCRAPER_ATTEMPTS) == 2


@pytest.mark.asyncio
async def test_chain_span_content_len_set_on_winning_result(
    recording_tracer: _RecordingTracer,
) -> None:
    text = "B" * 750
    provider = _FakeProvider("crawl4ai", _ok_result(text))
    chain = ContentScraperChain(providers=[provider], race_enabled=False)
    await _run_chain(chain, recording_tracer)

    chain_span = recording_tracer.span_by_name("scraper.chain")
    assert chain_span is not None
    assert chain_span.attrs.get(SCRAPER_CONTENT_LEN) == len(text)


@pytest.mark.asyncio
async def test_chain_span_attempts_set_on_exhaustion(recording_tracer: _RecordingTracer) -> None:
    a = _FakeProvider("scrapling", _error_result())
    b = _FakeProvider("crawl4ai", _error_result())
    chain = ContentScraperChain(providers=[a, b], race_enabled=False)
    await _run_chain(chain, recording_tracer)

    chain_span = recording_tracer.span_by_name("scraper.chain")
    assert chain_span is not None
    assert chain_span.attrs.get(SCRAPER_ATTEMPTS) == 2


# ---------------------------------------------------------------------------
# Prometheus metrics wiring via record_scraper_attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_scraper_attempt_called_on_success(
    recording_tracer: _RecordingTracer,
) -> None:
    provider = _FakeProvider("scrapling", _ok_result())
    chain = ContentScraperChain(providers=[provider], race_enabled=False)

    calls: list[dict] = []

    def _spy(*, provider: str, status: str) -> None:
        calls.append({"provider": provider, "status": status})

    with patch("app.adapters.content.scraper.chain.record_scraper_attempt", side_effect=_spy):
        await _run_chain(chain, recording_tracer)

    assert any(c == {"provider": "scrapling", "status": "success"} for c in calls)


@pytest.mark.asyncio
async def test_record_scraper_attempt_called_on_no_content(
    recording_tracer: _RecordingTracer,
) -> None:
    provider = _FakeProvider("scrapling", _error_result())
    fallback = _FakeProvider("crawl4ai", _ok_result())
    chain = ContentScraperChain(providers=[provider, fallback], race_enabled=False)

    calls: list[dict] = []

    def _spy(*, provider: str, status: str) -> None:
        calls.append({"provider": provider, "status": status})

    with patch("app.adapters.content.scraper.chain.record_scraper_attempt", side_effect=_spy):
        await _run_chain(chain, recording_tracer)

    assert any(c == {"provider": "scrapling", "status": "error"} for c in calls)


@pytest.mark.asyncio
async def test_record_scraper_attempt_called_on_exception(
    recording_tracer: _RecordingTracer,
) -> None:
    provider = _FakeProvider("scrapling", RuntimeError("boom"))
    fallback = _FakeProvider("crawl4ai", _ok_result())
    chain = ContentScraperChain(providers=[provider, fallback], race_enabled=False)

    calls: list[dict] = []

    def _spy(*, provider: str, status: str) -> None:
        calls.append({"provider": provider, "status": status})

    with patch("app.adapters.content.scraper.chain.record_scraper_attempt", side_effect=_spy):
        await _run_chain(chain, recording_tracer)

    assert any(c["provider"] == "scrapling" and c["status"] == "error" for c in calls)


@pytest.mark.asyncio
async def test_record_scraper_attempt_latency_called_on_success(
    recording_tracer: _RecordingTracer,
) -> None:
    provider = _FakeProvider("scrapling", _ok_result())
    chain = ContentScraperChain(providers=[provider], race_enabled=False)

    latency_calls: list[dict] = []

    def _spy(*, provider: str, latency_seconds: float) -> None:
        latency_calls.append({"provider": provider, "latency_seconds": latency_seconds})

    with patch(
        "app.adapters.content.scraper.chain.record_scraper_attempt_latency", side_effect=_spy
    ):
        await _run_chain(chain, recording_tracer)

    assert any(c["provider"] == "scrapling" and c["latency_seconds"] >= 0.0 for c in latency_calls)


@pytest.mark.asyncio
async def test_record_scraper_attempt_latency_called_on_error(
    recording_tracer: _RecordingTracer,
) -> None:
    provider = _FakeProvider("scrapling", RuntimeError("net error"))
    fallback = _FakeProvider("crawl4ai", _ok_result())
    chain = ContentScraperChain(providers=[provider, fallback], race_enabled=False)

    latency_calls: list[dict] = []

    def _spy(*, provider: str, latency_seconds: float) -> None:
        latency_calls.append({"provider": provider, "latency_seconds": latency_seconds})

    with patch(
        "app.adapters.content.scraper.chain.record_scraper_attempt_latency", side_effect=_spy
    ):
        await _run_chain(chain, recording_tracer)

    assert any(c["provider"] == "scrapling" for c in latency_calls)


# ---------------------------------------------------------------------------
# CancelledError path: outcome=cancelled and metrics still called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_span_outcome_cancelled_on_cancel(
    recording_tracer: _RecordingTracer,
) -> None:
    class _CancellingProvider(_FakeProvider):
        async def scrape_markdown(
            self, url: str, *, mobile: bool = True, request_id: int | None = None
        ) -> FirecrawlResult:
            raise asyncio.CancelledError

    provider = _CancellingProvider("scrapling", None)
    chain = ContentScraperChain(providers=[provider], race_enabled=False)

    attempt_calls: list[dict] = []

    def _spy_attempt(*, provider: str, status: str) -> None:
        attempt_calls.append({"provider": provider, "status": status})

    with patch(
        "app.adapters.content.scraper.chain.record_scraper_attempt", side_effect=_spy_attempt
    ):
        with pytest.raises(asyncio.CancelledError):
            await _run_chain(chain, recording_tracer)

    rung = recording_tracer.span_by_name("scraper.scrapling")
    assert rung is not None
    assert rung.attrs.get(SCRAPER_OUTCOME) == "cancelled"
    assert any(c == {"provider": "scrapling", "status": "skipped"} for c in attempt_calls)
