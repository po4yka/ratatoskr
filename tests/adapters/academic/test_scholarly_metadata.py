"""Tests for the academic open-access / scholarly-metadata fallback.

Pure helpers are tested directly; the network chain is tested with respx (no
live network) and a patched SSRF guard (no live DNS).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.adapters.academic import scholarly_metadata as sm
from app.adapters.academic.scholarly_metadata import (
    OAMetadata,
    best_oa_pdf,
    doi_for,
    fetch_oa_metadata,
    reconstruct_abstract,
    strip_jats,
    title_matches,
)
from app.adapters.academic.url_patterns import AcademicHost, AcademicPaperRef

pytestmark = pytest.mark.no_network


def _ref(host: AcademicHost, paper_id: str, version: str | None = None) -> AcademicPaperRef:
    return AcademicPaperRef(host=host, paper_id=paper_id, version=version)


async def _always_safe(url: str) -> tuple[bool, str | None]:
    return True, None


def _plain_client() -> httpx.AsyncClient:
    # A plain client so respx intercepts at the transport level (the real
    # make_safe_async_client path is covered by the ssrf test-suite).
    return httpx.AsyncClient()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_doi_for_per_host() -> None:
    assert doi_for(_ref(AcademicHost.ARXIV, "2301.00001")) == "10.48550/arXiv.2301.00001"
    assert doi_for(_ref(AcademicHost.SSRN, "6531478")) == "10.2139/ssrn.6531478"
    assert doi_for(_ref(AcademicHost.NBER, "w12345")) == "10.3386/w12345"
    assert doi_for(_ref(AcademicHost.OSF, "abcde")) == "10.31219/osf.io/abcde"
    # Aggregators have no deterministic DOI -> title search.
    assert doi_for(_ref(AcademicHost.REPEC, "x")) is None
    assert doi_for(_ref(AcademicHost.RESEARCHGATE, "x")) is None


def test_doi_for_drops_version() -> None:
    assert (
        doi_for(_ref(AcademicHost.ARXIV, "2301.00001", version="2")) == "10.48550/arXiv.2301.00001"
    )


def test_reconstruct_abstract_orders_by_position() -> None:
    inverted = {"world": [1], "Hello": [0], "again": [2, 4], "and": [3]}
    assert reconstruct_abstract(inverted) == "Hello world again and again"


def test_reconstruct_abstract_empty() -> None:
    assert reconstruct_abstract(None) is None
    assert reconstruct_abstract({}) is None


def test_strip_jats() -> None:
    raw = "<jats:title>Abstract</jats:title><jats:p>Hello &amp; welcome.</jats:p>"
    assert strip_jats(raw) == "Hello & welcome."
    assert strip_jats(None) is None
    assert strip_jats("") is None


def test_best_oa_pdf() -> None:
    assert best_oa_pdf({"is_oa": True, "best_oa_location": {"url_for_pdf": "http://x/p.pdf"}}) == (
        "http://x/p.pdf"
    )
    # Falls back to .url then oa_locations.
    assert best_oa_pdf({"is_oa": True, "best_oa_location": {"url": "http://x/landing"}}) == (
        "http://x/landing"
    )
    assert (
        best_oa_pdf(
            {
                "is_oa": True,
                "best_oa_location": {},
                "oa_locations": [{"url_for_pdf": "http://x/o.pdf"}],
            }
        )
        == "http://x/o.pdf"
    )
    # Not OA / missing -> None.
    assert best_oa_pdf({"is_oa": False, "best_oa_location": {"url_for_pdf": "http://x"}}) is None
    assert best_oa_pdf(None) is None


def test_title_matches_fuzzy() -> None:
    assert title_matches("Attention Is All You Need", "Attention is all you need") is True
    assert title_matches("Attention Is All You Need", "A totally different paper") is False
    assert title_matches(None, "x") is False
    assert title_matches("x", None) is False


# ---------------------------------------------------------------------------
# fetch_oa_metadata — provider chain
# ---------------------------------------------------------------------------


def _mock_all_404(respx_mock: respx.MockRouter) -> None:
    for host in (
        "https://api.openalex.org",
        "https://api.semanticscholar.org",
        "https://api.crossref.org",
        "https://api.unpaywall.org",
    ):
        respx_mock.get(url__startswith=host).mock(return_value=httpx.Response(404))


@pytest.mark.asyncio
@respx.mock
async def test_openalex_doi_hit_reconstructs_abstract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "is_url_safe_async", _always_safe)
    _mock_all_404(respx.mock)
    respx.get(url__startswith="https://api.openalex.org").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "https://openalex.org/W1",
                "title": "A Paper",
                "abstract_inverted_index": {"Hello": [0], "world": [1]},
                "best_oa_location": {"pdf_url": "https://oa.example/p.pdf"},
            },
        )
    )
    oa = await fetch_oa_metadata(
        _ref(AcademicHost.SSRN, "123"), contact_email="t@e.com", http_client_factory=_plain_client
    )
    assert isinstance(oa, OAMetadata)
    assert oa.abstract == "Hello world"
    assert oa.title == "A Paper"
    assert oa.source == "openalex"
    assert oa.oa_pdf_url == "https://oa.example/p.pdf"


@pytest.mark.asyncio
@respx.mock
async def test_openalex_404_falls_through_to_semantic_scholar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sm, "is_url_safe_async", _always_safe)
    _mock_all_404(respx.mock)
    respx.get(url__startswith="https://api.semanticscholar.org").mock(
        return_value=httpx.Response(
            200,
            json={
                "title": "S2 Title",
                "abstract": "S2 abstract body.",
                "tldr": {"text": "the gist"},
            },
        )
    )
    oa = await fetch_oa_metadata(
        _ref(AcademicHost.SSRN, "123"), contact_email="t@e.com", http_client_factory=_plain_client
    )
    assert oa is not None
    assert oa.abstract == "S2 abstract body."
    assert oa.tldr == "the gist"
    assert oa.source == "semantic_scholar"


@pytest.mark.asyncio
@respx.mock
async def test_tldr_captured_even_when_openalex_supplies_abstract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sm, "is_url_safe_async", _always_safe)
    _mock_all_404(respx.mock)
    respx.get(url__startswith="https://api.openalex.org").mock(
        return_value=httpx.Response(
            200,
            json={"id": "W1", "abstract_inverted_index": {"OA": [0], "abstract": [1]}},
        )
    )
    respx.get(url__startswith="https://api.semanticscholar.org").mock(
        return_value=httpx.Response(200, json={"tldr": {"text": "one-liner"}})
    )
    oa = await fetch_oa_metadata(
        _ref(AcademicHost.SSRN, "123"), contact_email="t@e.com", http_client_factory=_plain_client
    )
    assert oa is not None
    assert oa.abstract == "OA abstract"
    assert oa.source == "openalex"  # abstract source unchanged
    assert oa.tldr == "one-liner"  # tldr still harvested from S2


@pytest.mark.asyncio
@respx.mock
async def test_crossref_tertiary_jats_abstract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "is_url_safe_async", _always_safe)
    _mock_all_404(respx.mock)
    respx.get(url__startswith="https://api.crossref.org").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "title": ["CR Title"],
                    "abstract": "<jats:p>Crossref abstract.</jats:p>",
                }
            },
        )
    )
    oa = await fetch_oa_metadata(
        _ref(AcademicHost.SSRN, "123"), contact_email="t@e.com", http_client_factory=_plain_client
    )
    assert oa is not None
    assert oa.abstract == "Crossref abstract."
    assert oa.source == "crossref"


@pytest.mark.asyncio
@respx.mock
async def test_unpaywall_supplies_oa_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "is_url_safe_async", _always_safe)
    _mock_all_404(respx.mock)
    # OpenAlex gives an abstract but NO oa pdf, so Unpaywall is consulted.
    respx.get(url__startswith="https://api.openalex.org").mock(
        return_value=httpx.Response(200, json={"id": "W1", "abstract_inverted_index": {"a": [0]}})
    )
    respx.get(url__startswith="https://api.unpaywall.org").mock(
        return_value=httpx.Response(
            200, json={"is_oa": True, "best_oa_location": {"url_for_pdf": "https://oa/u.pdf"}}
        )
    )
    oa = await fetch_oa_metadata(
        _ref(AcademicHost.SSRN, "123"), contact_email="t@e.com", http_client_factory=_plain_client
    )
    assert oa is not None
    assert oa.oa_pdf_url == "https://oa/u.pdf"


@pytest.mark.asyncio
@respx.mock
async def test_all_providers_404_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "is_url_safe_async", _always_safe)
    _mock_all_404(respx.mock)
    oa = await fetch_oa_metadata(
        _ref(AcademicHost.SSRN, "123"), contact_email="t@e.com", http_client_factory=_plain_client
    )
    assert oa is None


@pytest.mark.asyncio
@respx.mock
async def test_timeout_falls_through_not_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "is_url_safe_async", _always_safe)
    _mock_all_404(respx.mock)
    respx.get(url__startswith="https://api.openalex.org").mock(
        side_effect=httpx.ConnectTimeout("slow")
    )
    # No provider yields anything -> None, and crucially no exception escapes.
    oa = await fetch_oa_metadata(
        _ref(AcademicHost.SSRN, "123"), contact_email="t@e.com", http_client_factory=_plain_client
    )
    assert oa is None


@pytest.mark.asyncio
@respx.mock
async def test_title_search_verified_by_fuzzy_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """A no-DOI host (ResearchGate) uses title search, gated by fuzzy verification."""
    monkeypatch.setattr(sm, "is_url_safe_async", _always_safe)
    _mock_all_404(respx.mock)
    respx.get(url__startswith="https://api.openalex.org").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "W1",
                        "title": "Deep Learning for Widgets",
                        "abstract_inverted_index": {"Widget": [0], "science": [1]},
                    }
                ]
            },
        )
    )
    # Matching title -> trusted.
    oa = await fetch_oa_metadata(
        _ref(AcademicHost.RESEARCHGATE, "999"),
        contact_email="t@e.com",
        title_hint="Deep Learning For Widgets",
        http_client_factory=_plain_client,
    )
    assert oa is not None
    assert oa.abstract == "Widget science"


@pytest.mark.asyncio
@respx.mock
async def test_title_search_near_miss_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "is_url_safe_async", _always_safe)
    _mock_all_404(respx.mock)
    respx.get(url__startswith="https://api.openalex.org").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "W1",
                        "title": "An Entirely Unrelated Paper",
                        "abstract_inverted_index": {"x": [0]},
                    }
                ]
            },
        )
    )
    oa = await fetch_oa_metadata(
        _ref(AcademicHost.RESEARCHGATE, "999"),
        contact_email="t@e.com",
        title_hint="Deep Learning for Widgets",
        http_client_factory=_plain_client,
    )
    assert oa is None


@pytest.mark.asyncio
@respx.mock
async def test_ssrf_blocked_provider_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _blocked(url: str) -> tuple[bool, str | None]:
        return False, "Private or reserved IP address"

    monkeypatch.setattr(sm, "is_url_safe_async", _blocked)
    # Even though respx would answer 200, the SSRF guard short-circuits the GET.
    respx.get(url__startswith="https://api.openalex.org").mock(
        return_value=httpx.Response(200, json={"id": "W1", "abstract_inverted_index": {"a": [0]}})
    )
    oa = await fetch_oa_metadata(
        _ref(AcademicHost.SSRN, "123"), contact_email="t@e.com", http_client_factory=_plain_client
    )
    assert oa is None


# ---------------------------------------------------------------------------
# Robustness / hardening (malformed provider data must never raise)
# ---------------------------------------------------------------------------


def test_reconstruct_abstract_ignores_malformed_positions() -> None:
    # Non-int positions and non-list values are skipped, not raised on.
    assert reconstruct_abstract({"a": [0], "b": ["x"], "c": "nope", "d": [1]}) == "a d"


def test_best_oa_pdf_rejects_non_http_scheme() -> None:
    assert (
        best_oa_pdf({"is_oa": True, "best_oa_location": {"url_for_pdf": "file:///etc/passwd"}})
        is None
    )
    assert (
        best_oa_pdf({"is_oa": True, "best_oa_location": {"url_for_pdf": "ftp://x/p.pdf"}}) is None
    )


def test_openalex_oa_pdf_falls_back_to_oa_url() -> None:
    assert (
        sm._openalex_oa_pdf({"best_oa_location": {}, "open_access": {"oa_url": "https://oa/x.pdf"}})
        == "https://oa/x.pdf"
    )
    # data: scheme rejected.
    assert (
        sm._openalex_oa_pdf({"best_oa_location": {"pdf_url": "data:application/pdf;base64,x"}})
        is None
    )


@pytest.mark.asyncio
@respx.mock
async def test_semantic_scholar_null_abstract_and_tldr_yields_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sm, "is_url_safe_async", _always_safe)
    _mock_all_404(respx.mock)
    respx.get(url__startswith="https://api.semanticscholar.org").mock(
        return_value=httpx.Response(200, json={"title": "T", "abstract": None, "tldr": None})
    )
    oa = await fetch_oa_metadata(
        _ref(AcademicHost.SSRN, "1"), contact_email="t@e.com", http_client_factory=_plain_client
    )
    assert oa is None  # title alone is not "usable" content


@pytest.mark.asyncio
@respx.mock
async def test_openalex_title_search_empty_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "is_url_safe_async", _always_safe)
    _mock_all_404(respx.mock)
    respx.get(url__startswith="https://api.openalex.org").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    oa = await fetch_oa_metadata(
        _ref(AcademicHost.RESEARCHGATE, "1"),
        contact_email="t@e.com",
        title_hint="Some Title",
        http_client_factory=_plain_client,
    )
    assert oa is None
