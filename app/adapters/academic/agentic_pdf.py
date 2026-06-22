"""Tier-2 agentic PDF download for academic hosts with no deterministic URL.

Tier 1 (``CloakBrowserProvider.fetch_pdf``) handles hosts whose PDF URL is known
in advance (SSRN / arXiv / NBER / OSF). For ResearchGate / RePEc / arbitrary
landing pages there is no deterministic rewrite, so this downloader renders the
live page in the stealth browser and autonomously picks the control that
downloads the paper — by default with a DOM heuristic over anchor/button text +
href patterns, optionally with an injected LLM picker for harder pages.

Opt-in and host-allowlisted at the config layer (``AcademicConfig``); this module
just executes the decision. Best-effort: every path degrades to ``None`` rather
than raising, so the academic extractor falls through to the OA/metadata fallback.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)

# Visible-text signals that a control downloads the paper (SSRN "Download This
# Paper", ResearchGate "Download full-text PDF", generic "View PDF" / "Download").
_DOWNLOAD_TEXT_RE = re.compile(
    r"download\s+(?:this\s+)?paper|download\s+(?:full[\s-]?text\s+)?pdf"
    r"|full[\s-]?text\s+pdf|view\s+pdf|\bdownload\b",
    re.IGNORECASE,
)
# href shapes that resolve to a PDF / download endpoint.
_PDF_HREF_RE = re.compile(r"\.pdf(?:$|\?)|/download(?:$|[/?])|delivery\.cfm", re.IGNORECASE)


def _heuristic_pick(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Rank scanned controls; return the best download candidate or ``None``.

    Scores combine an href that looks like a PDF/download endpoint (strongest
    signal), download-ish visible text, and a slight preference for anchors over
    buttons (anchors carry a directly-fetchable href). A minimum score avoids
    clicking an unrelated link on an ambiguous page.
    """
    best: dict[str, Any] | None = None
    best_score = 0
    for c in candidates:
        text = str(c.get("text") or "")
        href = str(c.get("href") or "")
        score = 0
        if href and _PDF_HREF_RE.search(href):
            score += 3
        if _DOWNLOAD_TEXT_RE.search(text):
            score += 2
        if c.get("tag") == "a" and href:
            score += 1
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= 2 else None


class AgenticPdfDownloader:
    """Autonomously locate and fetch a paper's download control via the stealth browser."""

    def __init__(
        self,
        browser: Any,
        *,
        llm_picker: Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any] | None]]
        | None = None,
    ) -> None:
        # ``browser`` is a CloakBrowserProvider exposing ``download_pdf_via_controls``.
        self._browser = browser
        # Optional override: an async picker (e.g. one flash-LLM call). When None,
        # the DOM heuristic is used. ponytail: heuristic-first; wire an LLM only if
        # real pages defeat it.
        self._llm_picker = llm_picker

    async def download(
        self, landing_url: str, *, max_bytes: int, correlation_id: str | None = None
    ) -> bytes | None:
        picker = self._llm_picker or self._pick
        raw = await self._browser.download_pdf_via_controls(
            landing_url, picker=picker, max_bytes=max_bytes
        )
        if raw is None:
            logger.info(
                "agentic_pdf_download_miss",
                extra={"landing_url": landing_url, "cid": correlation_id},
            )
        return raw

    @staticmethod
    async def _pick(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        return _heuristic_pick(candidates)
