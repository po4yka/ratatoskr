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
    AcademicPlatformExtractor,
    PDFDownloadError,
    _extract_abstract,
    _extract_title,
    _harvest_pdf_anchor,
    _landing_looks_like_paywall,
)
from app.adapters.academic.url_patterns import AcademicHost, AcademicPaperRef

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

    def fake_is_url_safe(url: str) -> tuple[bool, str | None]:
        if url.startswith("http://127.0.0.1"):
            return False, "Private or reserved IP address: 127.0.0.1"
        return True, None

    monkeypatch.setattr("app.adapters.academic.platform_extractor.is_url_safe", fake_is_url_safe)

    with pytest.raises(PDFDownloadError, match="ssrf_blocked"):
        await extractor._download_pdf("https://example.com/paper.pdf")


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
