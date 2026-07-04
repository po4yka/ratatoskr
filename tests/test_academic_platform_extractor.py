"""Unit tests for academic platform-extractor helpers.

The orchestrator's end-to-end network path is exercised manually via
``python -m app.cli.summary --url <arxiv-or-ssrn-url>``. These tests
cover the pure-function helpers (markdown parsing, paywall detection,
PDF anchor harvesting) and the orchestrator's branching logic via a
fake scraper + fake HTTP client.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.adapters.academic.platform_extractor import (
    AcademicPaperUnavailableError,
    AcademicPlatformExtractor,
    PDFDownloadError,
    _extract_abstract,
    _extract_pdf_text_from_bytes,
    _extract_title,
    _harvest_pdf_anchor,
    _is_synthetic_title,
    _landing_looks_like_paywall,
)
from app.adapters.academic.scholarly_metadata import OAMetadata
from app.adapters.academic.url_patterns import AcademicHost, AcademicPaperRef
from app.adapters.content.platform_extraction.models import PlatformExtractionRequest
from app.config.academic import AcademicConfig
from app.core.call_status import CallStatus

# ---------------------------------------------------------------------------
# _extract_abstract
# ---------------------------------------------------------------------------


def test_extract_abstract_after_h2_heading() -> None:
    md = (
        "# Some Paper Title\n\n"
        "## Abstract\n\n"
        "This is the abstract text. It spans multiple sentences.\n\n"
        "## Introduction\n\n"
        "Body content here."
    )
    assert _extract_abstract(md) == ("This is the abstract text. It spans multiple sentences.")


def test_extract_abstract_after_h3_heading() -> None:
    md = "### Abstract\n\nShort abstract.\n\n### References\n\nrefs..."
    assert _extract_abstract(md) == "Short abstract."


def test_extract_abstract_case_insensitive_label() -> None:
    md = "## ABSTRACT:\n\nUppercase label with colon.\n\n## Method\n\nbody"
    assert _extract_abstract(md) == "Uppercase label with colon."


def test_extract_abstract_returns_none_when_missing() -> None:
    md = "# Title\n\nNo abstract section here.\n\n## Method\n\nbody"
    assert _extract_abstract(md) is None


def test_extract_abstract_when_no_subsequent_heading() -> None:
    """Last section in a doc — abstract runs to EOF."""
    md = "## Abstract\n\nAbstract that runs to end of doc."
    assert _extract_abstract(md) == "Abstract that runs to end of doc."


def test_extract_abstract_handles_empty_markdown() -> None:
    assert _extract_abstract("") is None
    assert _extract_abstract(None) is None


# ---------------------------------------------------------------------------
# _extract_title
# ---------------------------------------------------------------------------


def test_extract_title_from_scraper_metadata() -> None:
    ref = AcademicPaperRef(host=AcademicHost.ARXIV, paper_id="2301.00001")
    title = _extract_title(ref, "", {"title": "The Jevons Paradox"})
    assert title == "The Jevons Paradox"


def test_extract_title_from_first_h1() -> None:
    ref = AcademicPaperRef(host=AcademicHost.SSRN, paper_id="6531478")
    md = "# A Paper Title\n\nbody"
    assert _extract_title(ref, md, {}) == "A Paper Title"


def test_extract_title_falls_back_to_host_and_id() -> None:
    ref = AcademicPaperRef(host=AcademicHost.NBER, paper_id="w12345")
    title = _extract_title(ref, "no headings here", {})
    assert title == "NBER paper w12345"


def test_extract_title_prefers_metadata_over_h1() -> None:
    ref = AcademicPaperRef(host=AcademicHost.ARXIV, paper_id="2301.00001")
    md = "# H1 Title\n\nbody"
    title = _extract_title(ref, md, {"title": "Metadata Title"})
    assert title == "Metadata Title"


# ---------------------------------------------------------------------------
# _harvest_pdf_anchor
# ---------------------------------------------------------------------------


def test_harvest_pdf_anchor_basic() -> None:
    md = "Some text.\n\n[Open PDF in Browser](https://example.com/paper.pdf)\n\nMore text."
    assert _harvest_pdf_anchor(md) == "https://example.com/paper.pdf"


def test_harvest_pdf_anchor_with_query_string() -> None:
    md = "[Download](https://example.com/paper.pdf?abstractid=123&mirid=1)"
    url = _harvest_pdf_anchor(md)
    assert url is not None
    assert url.endswith("paper.pdf?abstractid=123&mirid=1")


def test_harvest_pdf_anchor_picks_first_match() -> None:
    """When multiple PDF anchors exist (e.g. paper + supplements), take the first."""
    md = "[Main paper](https://example.com/main.pdf)\n\n[Supplement](https://example.com/supp.pdf)"
    assert _harvest_pdf_anchor(md) == "https://example.com/main.pdf"


def test_harvest_pdf_anchor_returns_none_when_absent() -> None:
    assert _harvest_pdf_anchor("no anchors here") is None
    assert _harvest_pdf_anchor("") is None


# ---------------------------------------------------------------------------
# _landing_looks_like_paywall
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker",
    [
        "Purchase to read",
        "Request full-text",
        "Sign in to download",
    ],
)
def test_landing_paywall_detected(marker: str) -> None:
    md = f"Some preamble. {marker} the full paper. Trailing text."
    assert _landing_looks_like_paywall(md) is True


def test_landing_not_paywall_for_open_access() -> None:
    md = "Abstract: a fully open paper. Download free."
    assert _landing_looks_like_paywall(md) is False


def test_landing_paywall_empty_input() -> None:
    assert _landing_looks_like_paywall("") is False


# ---------------------------------------------------------------------------
# AcademicPlatformExtractor.supports() — predicate sanity check
# ---------------------------------------------------------------------------


def test_supports_delegates_to_url_parser() -> None:
    """The supports() predicate must mirror parse_academic_paper_url
    so the platform router doesn't divert non-academic URLs."""
    # Minimal stub deps — supports() doesn't touch them.
    extractor = AcademicPlatformExtractor(
        cfg=None,
        scraper=None,
        firecrawl_sem=_NullSem(),
        lifecycle=None,
    )
    assert extractor.supports("https://arxiv.org/abs/2301.00001") is True
    assert extractor.supports("https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1") is True
    assert extractor.supports("https://example.com/article") is False
    assert extractor.supports("https://github.com/foo/bar") is False


@pytest.mark.asyncio
async def test_download_pdf_blocks_private_redirect_target(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = AcademicPlatformExtractor(
        cfg=None,
        scraper=None,
        firecrawl_sem=_NullSem(),
        lifecycle=None,
        http_client_factory=lambda: _FakeHTTPClient(
            [
                httpx.Response(
                    302,
                    headers={"location": "http://127.0.0.1/private.pdf"},
                    request=httpx.Request("GET", "https://example.com/paper.pdf"),
                )
            ]
        ),
    )

    async def fake_is_url_safe_async(url: str) -> tuple[bool, str | None]:
        if url.startswith("http://127.0.0.1"):
            return False, "Private or reserved IP address: 127.0.0.1"
        return True, None

    monkeypatch.setattr(
        "app.adapters.academic.platform_extractor.is_url_safe_async", fake_is_url_safe_async
    )

    with pytest.raises(PDFDownloadError, match="ssrf_blocked"):
        await extractor._download_pdf("https://example.com/paper.pdf")


# ---------------------------------------------------------------------------
# _extract_pdf_text_from_bytes — decompression-bomb guard
# ---------------------------------------------------------------------------


def test_extract_pdf_text_aborts_when_aggregate_text_exceeds_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PDF whose decompressed text exceeds the aggregate cap must abort
    with PDFDownloadError, not silently return an unbounded string.

    The download-size cap (50 MB compressed) does not bound decompressed
    text -- a crafted FlateDecode stream can still expand far beyond that,
    so the extraction loop needs its own independent guard.
    """
    fitz = pytest.importorskip("fitz", reason="PyMuPDF (fitz) required")

    monkeypatch.setattr(
        "app.adapters.academic.platform_extractor._DEFAULT_PDF_MAX_TEXT_CHARS", 50
    )

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "x" * 200)
    pdf_bytes = doc.tobytes()
    doc.close()

    with pytest.raises(PDFDownloadError, match="pdf_text_too_large"):
        _extract_pdf_text_from_bytes(pdf_bytes)


def test_extract_pdf_text_within_cap_returns_full_text() -> None:
    """Normal-sized PDFs are unaffected by the new aggregate cap."""
    fitz = pytest.importorskip("fitz", reason="PyMuPDF (fitz) required")

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "hello world")
    pdf_bytes = doc.tobytes()
    doc.close()

    text, pages = _extract_pdf_text_from_bytes(pdf_bytes)
    assert "hello world" in text
    assert pages == 1


class _NullSem:
    """Minimal async context-manager so supports() can be constructed."""

    def __call__(self) -> _NullSem:
        return self

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeStreamCtx:
    """Async context manager wrapping a pre-built httpx.Response for stream()."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def __aenter__(self) -> httpx.Response:
        return self._response

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeHTTPClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)

    async def __aenter__(self) -> _FakeHTTPClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs: Any) -> _FakeStreamCtx:
        if not self._responses:
            raise AssertionError(f"unexpected {method} {url}")
        return _FakeStreamCtx(self._responses.pop(0))

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        if not self._responses:
            raise AssertionError(f"unexpected GET {url}")
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# Open-access / metadata fallback (extract() wiring + _recover_via_oa_metadata)
# ---------------------------------------------------------------------------


class _Cfg:
    def __init__(self, academic: AcademicConfig) -> None:
        self.academic = academic


class _FakeCrawl:
    """Minimal stand-in for a scraper-chain crawl result."""

    def __init__(self, markdown: str = "", metadata: dict[str, Any] | None = None) -> None:
        self.status = CallStatus.OK
        self.content_markdown = markdown
        self.metadata_json = metadata or {}


class _FakeScraper:
    def __init__(self, crawl: _FakeCrawl) -> None:
        self._crawl = crawl

    async def scrape_markdown(self, url: str, *, request_id: int | None = None) -> _FakeCrawl:
        return self._crawl


def _ssrn_request() -> PlatformExtractionRequest:
    url = "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6531478"
    return PlatformExtractionRequest(
        message=None,
        url_text=url,
        normalized_url=url,
        correlation_id="cid-int",
        request_id_override=1,
        mode="pure",
    )


def _gated_extractor(
    academic: AcademicConfig,
    monkeypatch: pytest.MonkeyPatch,
    *,
    browser_pdf: Any | None = None,
    agentic_pdf: Any | None = None,
) -> AcademicPlatformExtractor:
    """Extractor whose landing scrape is empty and whose SSRN PDF 403s -> gated."""
    async def fake_is_url_safe_async(u: str) -> tuple[bool, str | None]:
        return True, None

    monkeypatch.setattr(
        "app.adapters.academic.platform_extractor.is_url_safe_async", fake_is_url_safe_async
    )
    return AcademicPlatformExtractor(
        cfg=_Cfg(academic),
        scraper=_FakeScraper(_FakeCrawl(markdown="")),
        firecrawl_sem=_NullSem(),
        lifecycle=None,
        browser_pdf=browser_pdf,
        agentic_pdf=agentic_pdf,
        http_client_factory=lambda: _FakeHTTPClient(
            [httpx.Response(403, request=httpx.Request("GET", "https://papers.ssrn.com/D.pdf"))]
        ),
    )


class _FakeBrowserPdf:
    """Tier-1 fake: records calls and returns canned bytes (or None for a miss)."""

    def __init__(self, pdf: bytes | None) -> None:
        self._pdf = pdf
        self.calls: list[tuple[str, str]] = []

    async def fetch_pdf(
        self, landing_url: str, pdf_url: str, *, max_bytes: int, mobile: bool = False
    ) -> bytes | None:
        self.calls.append((landing_url, pdf_url))
        return self._pdf


class _FakeAgenticPdf:
    """Tier-2 fake: records the landing it was asked to recover."""

    def __init__(self, pdf: bytes | None) -> None:
        self._pdf = pdf
        self.calls: list[str] = []

    async def download(
        self, landing_url: str, *, max_bytes: int, correlation_id: str | None = None
    ) -> bytes | None:
        self.calls.append(landing_url)
        return self._pdf


def _researchgate_request() -> PlatformExtractionRequest:
    url = "https://www.researchgate.net/publication/123456_Some_Paper"
    return PlatformExtractionRequest(
        message=None,
        url_text=url,
        normalized_url=url,
        correlation_id="cid-rg",
        request_id_override=2,
        mode="pure",
    )


@pytest.mark.asyncio
async def test_fallback_disabled_raises_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = _gated_extractor(AcademicConfig(), monkeypatch)
    with pytest.raises(AcademicPaperUnavailableError) as exc:
        await extractor.extract(_ssrn_request())
    # The SSRN PDF 403'd -> paywall reason preserved; no fallback attempted.
    assert exc.value.reason == "paywall"


@pytest.mark.asyncio
async def test_fallback_recovers_abstract(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = _gated_extractor(
        AcademicConfig(metadata_fallback_enabled=True, contact_email="t@e.com"), monkeypatch
    )

    async def _fake_fetch(ref: Any, **kwargs: Any) -> OAMetadata:
        return OAMetadata(
            title="Recovered Title",
            abstract="Recovered abstract body.",
            tldr=None,
            doi="10.2139/ssrn.6531478",
            oa_pdf_url=None,
            source="openalex",
        )

    monkeypatch.setattr("app.adapters.academic.platform_extractor.fetch_oa_metadata", _fake_fetch)
    result = await extractor.extract(_ssrn_request())
    assert result.content_source == "academic_metadata_fallback_used"
    assert "## Abstract\n\nRecovered abstract body." in result.content_text
    assert result.title == "Recovered Title"
    assert result.metadata["metadata_fallback_used"] is True
    assert result.metadata["metadata_provider"] == "openalex"
    assert result.metadata["oa_pdf_used"] is False


@pytest.mark.asyncio
async def test_fallback_all_providers_empty_raises_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extractor = _gated_extractor(
        AcademicConfig(metadata_fallback_enabled=True, contact_email="t@e.com"), monkeypatch
    )

    async def _fake_fetch(ref: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.adapters.academic.platform_extractor.fetch_oa_metadata", _fake_fetch)
    with pytest.raises(AcademicPaperUnavailableError) as exc:
        await extractor.extract(_ssrn_request())
    assert exc.value.reason == "metadata_fallback_exhausted"


@pytest.mark.asyncio
async def test_recover_oa_pdf_runs_through_existing_extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extractor = _gated_extractor(
        AcademicConfig(metadata_fallback_enabled=True, contact_email="t@e.com"), monkeypatch
    )

    async def _fake_fetch(ref: Any, **kwargs: Any) -> OAMetadata:
        return OAMetadata(
            title=None,
            abstract="An abstract.",
            tldr=None,
            doi="10.x",
            oa_pdf_url="https://oa.example/paper.pdf",
            source="openalex",
        )

    async def _fake_pdf(url: str) -> tuple[str, int]:
        assert url == "https://oa.example/paper.pdf"
        return "Full body text.", 7

    monkeypatch.setattr("app.adapters.academic.platform_extractor.fetch_oa_metadata", _fake_fetch)
    monkeypatch.setattr(extractor, "_fetch_and_extract_pdf", _fake_pdf)

    ref = AcademicPaperRef(host=AcademicHost.SSRN, paper_id="6531478")
    recovery = await extractor._recover_via_oa_metadata(ref, title_hint=None, correlation_id="c")
    assert recovery is not None
    # OA-PDF-first: a full body beats the abstract.
    assert recovery.content_source == "academic_oa_pdf_used"
    assert recovery.pdf_text == "Full body text."
    assert recovery.pdf_pages == 7
    assert recovery.abstract is None


@pytest.mark.asyncio
async def test_recover_oa_pdf_failure_falls_back_to_abstract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extractor = _gated_extractor(
        AcademicConfig(metadata_fallback_enabled=True, contact_email="t@e.com"), monkeypatch
    )

    async def _fake_fetch(ref: Any, **kwargs: Any) -> OAMetadata:
        return OAMetadata(
            title=None,
            abstract="Backup abstract.",
            tldr=None,
            doi="10.x",
            oa_pdf_url="https://oa.example/gated.pdf",
            source="semantic_scholar",
        )

    async def _failing_pdf(url: str) -> tuple[str, int]:
        raise PDFDownloadError("paywall")

    monkeypatch.setattr("app.adapters.academic.platform_extractor.fetch_oa_metadata", _fake_fetch)
    monkeypatch.setattr(extractor, "_fetch_and_extract_pdf", _failing_pdf)

    ref = AcademicPaperRef(host=AcademicHost.SSRN, paper_id="6531478")
    recovery = await extractor._recover_via_oa_metadata(ref, title_hint=None, correlation_id="c")
    assert recovery is not None
    assert recovery.content_source == "academic_metadata_fallback_used"
    assert recovery.pdf_text is None
    assert recovery.abstract == "Backup abstract."


def test_is_synthetic_title() -> None:
    ref = AcademicPaperRef(host=AcademicHost.NBER, paper_id="w12345")
    assert _is_synthetic_title(None, ref) is True
    assert _is_synthetic_title("NBER paper w12345", ref) is True
    assert _is_synthetic_title("A Real Title", ref) is False


@pytest.mark.asyncio
async def test_extract_fallback_oa_pdf_fails_uses_abstract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full extract(): OA PDF URL present but PDF extraction fails -> abstract used."""
    extractor = _gated_extractor(
        AcademicConfig(metadata_fallback_enabled=True, contact_email="t@e.com"), monkeypatch
    )

    async def _fake_fetch(ref: Any, **kwargs: Any) -> OAMetadata:
        return OAMetadata(
            title="Recovered Title",
            abstract="Recovered abstract body.",
            tldr=None,
            doi="10.2139/ssrn.6531478",
            oa_pdf_url="https://oa.example/gated.pdf",
            source="openalex",
        )

    async def _failing_pdf(url: str) -> tuple[str, int]:
        raise PDFDownloadError("paywall")

    monkeypatch.setattr("app.adapters.academic.platform_extractor.fetch_oa_metadata", _fake_fetch)
    monkeypatch.setattr(extractor, "_fetch_and_extract_pdf", _failing_pdf)

    result = await extractor.extract(_ssrn_request())
    assert result.content_source == "academic_metadata_fallback_used"
    assert "## Abstract\n\nRecovered abstract body." in result.content_text
    assert result.metadata["metadata_fallback_used"] is True
    assert result.metadata["oa_pdf_used"] is False
    assert result.metadata["pdf_extracted"] is False


# ---------------------------------------------------------------------------
# Browser PDF recovery (tier 1 deterministic + tier 2 agentic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_pdf_tier1_recovers_full_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gated SSRN PDF is recovered through the stealth session -> full body."""
    browser = _FakeBrowserPdf(b"%PDF-1.7 fake bytes")
    extractor = _gated_extractor(
        AcademicConfig(browser_pdf_recovery_enabled=True), monkeypatch, browser_pdf=browser
    )
    monkeypatch.setattr(
        "app.adapters.academic.platform_extractor._extract_pdf_text_from_bytes",
        lambda raw: ("Recovered body.", 5),
    )

    result = await extractor.extract(_ssrn_request())

    assert result.content_source == "academic_paper_full"
    assert "## Body\n\nRecovered body." in result.content_text
    assert result.metadata["pdf_browser_recovery_used"] is True
    assert result.metadata["pdf_browser_recovery_tier"] == "deterministic"
    assert result.metadata["pdf_pages_extracted"] == 5
    # Fetched the deterministic SSRN Delivery.cfm URL through the landing session.
    assert len(browser.calls) == 1
    landing, pdf_url = browser.calls[0]
    assert "abstract_id=6531478" in landing
    assert "Delivery.cfm" in pdf_url


@pytest.mark.asyncio
async def test_browser_pdf_recovery_disabled_does_not_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off -> the browser is never invoked and the paper fails as before."""
    browser = _FakeBrowserPdf(b"%PDF-1.7 fake")
    extractor = _gated_extractor(AcademicConfig(), monkeypatch, browser_pdf=browser)
    with pytest.raises(AcademicPaperUnavailableError) as exc:
        await extractor.extract(_ssrn_request())
    assert exc.value.reason == "paywall"
    assert browser.calls == []


@pytest.mark.asyncio
async def test_browser_pdf_miss_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browser miss (None) degrades to the existing unavailable error."""
    browser = _FakeBrowserPdf(None)
    extractor = _gated_extractor(
        AcademicConfig(browser_pdf_recovery_enabled=True), monkeypatch, browser_pdf=browser
    )
    with pytest.raises(AcademicPaperUnavailableError):
        await extractor.extract(_ssrn_request())
    assert len(browser.calls) == 1


@pytest.mark.asyncio
async def test_browser_pdf_tier2_agentic_for_no_deterministic_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ResearchGate has no deterministic URL -> tier-2 agentic download fires."""
    agentic = _FakeAgenticPdf(b"%PDF-1.7 fake")
    extractor = _gated_extractor(
        AcademicConfig(
            browser_pdf_recovery_enabled=True,
            agentic_pdf_download_enabled=True,
            agentic_pdf_host_allowlist=("researchgate",),
        ),
        monkeypatch,
        browser_pdf=_FakeBrowserPdf(None),
        agentic_pdf=agentic,
    )
    monkeypatch.setattr(
        "app.adapters.academic.platform_extractor._extract_pdf_text_from_bytes",
        lambda raw: ("RG body.", 3),
    )

    result = await extractor.extract(_researchgate_request())

    assert result.content_source == "academic_paper_full"
    assert result.metadata["pdf_browser_recovery_tier"] == "agentic"
    assert "## Body\n\nRG body." in result.content_text
    assert len(agentic.calls) == 1


@pytest.mark.asyncio
async def test_browser_pdf_tier2_skipped_when_host_not_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agentic tier honors the host allowlist: a non-listed host is not attempted."""
    agentic = _FakeAgenticPdf(b"%PDF-1.7 fake")
    extractor = _gated_extractor(
        AcademicConfig(
            agentic_pdf_download_enabled=True,
            agentic_pdf_host_allowlist=("repec",),  # NOT researchgate
        ),
        monkeypatch,
        agentic_pdf=agentic,
    )
    with pytest.raises(AcademicPaperUnavailableError):
        await extractor.extract(_researchgate_request())
    assert agentic.calls == []
