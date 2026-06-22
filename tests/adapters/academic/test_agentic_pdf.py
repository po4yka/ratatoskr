"""Tests for the tier-2 agentic PDF downloader (heuristic picker + browser glue)."""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.academic.agentic_pdf import AgenticPdfDownloader, _heuristic_pick

pytestmark = pytest.mark.no_network


def test_heuristic_prefers_pdf_href_anchor() -> None:
    candidates = [
        {"tag": "a", "text": "Home", "href": "https://x/"},
        {"tag": "a", "text": "Open", "href": "https://x/paper.pdf"},
    ]
    pick = _heuristic_pick(candidates)
    assert pick is not None and pick["href"].endswith("paper.pdf")


def test_heuristic_matches_download_this_paper_text() -> None:
    candidates = [
        {"tag": "button", "text": "Download This Paper", "href": None},
        {"tag": "a", "text": "Cite", "href": "https://x/cite"},
    ]
    pick = _heuristic_pick(candidates)
    assert pick is not None and pick["text"] == "Download This Paper"


def test_heuristic_scores_delivery_cfm_and_download_path() -> None:
    candidates = [
        {"tag": "a", "text": "x", "href": "https://x/sol3/Delivery.cfm?abstractid=1"},
        {"tag": "a", "text": "y", "href": "https://x/files/1/download"},
    ]
    # Both look like download endpoints; the first scores highest (href + anchor).
    assert _heuristic_pick(candidates) is not None


def test_heuristic_returns_none_on_ambiguous_page() -> None:
    candidates = [
        {"tag": "a", "text": "About", "href": "https://x/about"},
        {"tag": "button", "text": "Sign in", "href": None},
    ]
    assert _heuristic_pick(candidates) is None


def test_heuristic_handles_empty() -> None:
    assert _heuristic_pick([]) is None


class _FakeBrowser:
    """Stand-in for CloakBrowserProvider.download_pdf_via_controls."""

    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self._candidates = candidates
        self.picked: dict[str, Any] | None = None

    async def download_pdf_via_controls(
        self, landing_url: str, *, picker: Any, max_bytes: int, mobile: bool = False
    ) -> bytes | None:
        self.picked = await picker(self._candidates)
        return b"%PDF-1.7 ok" if self.picked else None


@pytest.mark.asyncio
async def test_download_uses_heuristic_picker() -> None:
    browser = _FakeBrowser([{"tag": "a", "text": "Download PDF", "href": "https://x/p.pdf"}])
    dl = AgenticPdfDownloader(browser)
    out = await dl.download("https://x/landing", max_bytes=1_000_000)
    assert out == b"%PDF-1.7 ok"
    assert browser.picked is not None and browser.picked["href"] == "https://x/p.pdf"


@pytest.mark.asyncio
async def test_download_returns_none_on_ambiguous_page() -> None:
    browser = _FakeBrowser([{"tag": "a", "text": "About", "href": "https://x/about"}])
    out = await AgenticPdfDownloader(browser).download("https://x/landing", max_bytes=1_000)
    assert out is None


@pytest.mark.asyncio
async def test_injected_llm_picker_overrides_heuristic() -> None:
    chosen = {"tag": "button", "text": "Get the manuscript", "href": None}

    async def _llm_picker(_candidates: list[dict[str, Any]]) -> dict[str, Any]:
        return chosen

    browser = _FakeBrowser([{"tag": "a", "text": "About", "href": "https://x/about"}])
    dl = AgenticPdfDownloader(browser, llm_picker=_llm_picker)
    out = await dl.download("https://x/landing", max_bytes=1_000)
    assert out == b"%PDF-1.7 ok"
    assert browser.picked == chosen
