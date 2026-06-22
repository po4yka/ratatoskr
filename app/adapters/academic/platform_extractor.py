"""Platform extractor for academic-paper landing pages.

Flow (one ``extract`` call):

1. Recognize the URL via ``parse_academic_paper_url`` (already done at
   the predicate layer, but the parsed ref is needed here too).
2. Compute a canonical landing URL and create / dedupe the
   ``requests`` row through the shared lifecycle helper.
3. Fetch the landing HTML via the scraper chain — this transparently
   uses Scrapling / patchright stealth / Firecrawl, so Cloudflare-gated
   hosts (SSRN, ResearchGate) clear the challenge here.
4. Harvest the abstract + title from the landing markdown using a
   generic ``# Abstract`` heading heuristic (every host we support
   surfaces an "Abstract" heading after markdown conversion).
5. Resolve the PDF URL — deterministic rewrite when available
   (arXiv, SSRN, NBER, OSF), anchor discovery from the landing markdown
   otherwise (ResearchGate, RePEc).
6. Download the PDF via httpx, extract body text via the existing
   ``PDFExtractor`` (pymupdf), and concatenate ``[Abstract][Body]``.
7. Paywall / 403 / network failure on the PDF leg degrades gracefully
   to an abstract-only summary with an explicit
   ``[PDF unavailable: <reason>]`` note in ``content_text`` — never a
   generic ``Content Extraction Failed`` error when we have the
   abstract in hand. This matches the resolved decision from the
   2026-05-13 design interview.
"""

from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import httpx

from app.adapters.academic.resolvers import landing_url_for, pdf_url_for
from app.adapters.academic.scholarly_metadata import fetch_oa_metadata
from app.adapters.academic.url_patterns import (
    AcademicPaperRef,
    is_academic_paper_url,
    parse_academic_paper_url,
)
from app.adapters.content.platform_extraction.models import (
    PlatformExtractionRequest,
    PlatformExtractionResult,
)
from app.adapters.content.platform_extraction.protocol import PlatformExtractor
from app.core.call_status import CallStatus
from app.core.lang import detect_language
from app.core.logging_utils import get_logger
from app.core.url_utils import compute_dedupe_hash
from app.security.ssrf import is_url_safe, make_safe_async_client

if TYPE_CHECKING:
    from app.adapters.content.platform_extraction.lifecycle import PlatformRequestLifecycle

logger = get_logger(__name__)

# Effectively disabled per the 2026-05-13 design decision: the downstream
# LLM caller owns the token-budget cap via instructor's chat_structured.
# We still cap absurdly long inputs (book-length reports, multi-paper
# compilations) so one request can't exhaust pymupdf memory on the Pi.
_DEFAULT_PDF_MAX_PAGES = 1000

# Generous timeout for academic PDFs: arXiv preprints are ~1-5 MB,
# SSRN papers up to ~15 MB, but Cloudflare-gated hosts can be slow.
_PDF_DOWNLOAD_TIMEOUT_SEC = 60.0

# Hard memory-protection cap: abort streaming download when the running
# total exceeds this limit.  50 MB covers the largest real academic PDFs
# (multi-chapter NBER working papers) while bounding the in-process
# allocation on the Pi.  Override via AcademicPlatformExtractor(max_pdf_mb=N).
_DEFAULT_MAX_PDF_MB = 50

# Markers we look for in the landing HTML to detect a paywall response
# (matches SSRN's "purchase to read" upsell and ResearchGate's
# "Request full-text" gate).
_PAYWALL_HTML_MARKERS = (
    "purchase to read",
    "buy this paper",
    "request full-text",
    "request the full-text",
    "sign in to download",
    "you need to be a member",
)


class PDFDownloadError(Exception):
    """PDF acquisition failed; ``reason`` is a short, user-facing tag."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AcademicPaperUnavailableError(Exception):
    """Neither the abstract nor the PDF body could be reached.

    Distinct from a generic ``ValueError`` so the URL-flow's catch-all
    error handler can render a meaningful 'paywalled paper' diagnostic
    instead of the generic 'AI models returned data that couldn't be
    parsed' message that fires for actual LLM failures.

    ``reason`` is a short tag (``paywall``, ``network_error``,
    ``pdf_not_found``, etc.) suitable for telemetry and template
    selection; ``host`` is the host enum value (e.g. ``ssrn``).
    """

    def __init__(self, *, reason: str, host: str, url: str) -> None:
        super().__init__(f"Academic paper unavailable (host={host}, reason={reason}): {url}")
        self.reason = reason
        self.host = host
        self.url = url


@dataclass(frozen=True)
class _OARecovery:
    """Additive result of the open-access / metadata fallback.

    Only ever produced when the landing scrape yielded nothing, so every field
    is purely recovered content the extractor layers on before composing.
    """

    title: str | None
    abstract: str | None
    pdf_text: str | None
    pdf_pages: int
    pdf_url: str | None
    content_source: str
    source: str | None


class AcademicPlatformExtractor(PlatformExtractor):
    """First-class extractor for SSRN / arXiv / NBER / OSF / RePEc / ResearchGate."""

    def __init__(
        self,
        *,
        cfg: Any,
        scraper: Any,
        firecrawl_sem: Any,
        lifecycle: PlatformRequestLifecycle,
        http_client_factory: Any | None = None,
        max_pdf_mb: int = _DEFAULT_MAX_PDF_MB,
    ) -> None:
        self._cfg = cfg
        self._scraper = scraper
        self._firecrawl_sem = firecrawl_sem
        self._lifecycle = lifecycle
        # Injectable for tests; defaults to an SSRF-safe httpx client with
        # manual redirect handling (OSF /download 302s).
        self._http_client_factory = http_client_factory
        self._max_pdf_bytes = max_pdf_mb * 1024 * 1024

    # ------------------------------------------------------------------
    # PlatformExtractor protocol
    # ------------------------------------------------------------------

    def supports(self, normalized_url: str) -> bool:
        return is_academic_paper_url(normalized_url)

    async def extract(self, request: PlatformExtractionRequest) -> PlatformExtractionResult:
        ref = parse_academic_paper_url(request.normalized_url)
        if ref is None:
            msg = f"Cannot parse academic paper URL: {request.normalized_url!r}"
            raise ValueError(msg)

        canonical_landing = landing_url_for(ref) or request.normalized_url

        # 1. Dedupe / create the request row. paper_canonical_id is the
        # authoritative dedupe key (collapses /abs/X and /pdf/X.pdf,
        # v1 and v2, into one row); dedupe_hash on the canonical
        # landing URL is kept as a secondary key for symmetry with
        # non-academic flows.
        dedupe_hash = compute_dedupe_hash(canonical_landing)
        request_id = request.request_id_override
        if request.mode == "interactive":
            await self._lifecycle.send_accepted_notification(request)
            request_id = await self._lifecycle.handle_request_dedupe_or_create(
                request,
                dedupe_hash=dedupe_hash,
                paper_canonical_id=ref.canonical_id,
            )

        # 2. Fetch landing HTML via the scraper chain (uses patchright
        # stealth for Cloudflare-gated hosts).
        async with self._firecrawl_sem():
            landing_crawl = await self._scraper.scrape_markdown(
                canonical_landing,
                request_id=request_id,
            )

        landing_markdown = self._extract_markdown(landing_crawl)
        landing_metadata = (
            landing_crawl.metadata_json
            if isinstance(getattr(landing_crawl, "metadata_json", None), dict)
            else {}
        )

        title = _extract_title(ref, landing_markdown, landing_metadata)
        abstract = _extract_abstract(landing_markdown)

        # 3. Resolve a PDF URL — deterministic rewrite first, anchor
        # discovery as fallback for hosts that need it.
        pdf_url = pdf_url_for(ref) or _harvest_pdf_anchor(landing_markdown)

        # 4. Try to acquire and extract the PDF body. Paywall / 403 /
        # network failure degrades to abstract-only, never to a hard
        # extraction error.
        pdf_text: str | None = None
        pdf_failure_reason: str | None = None
        pages_extracted: int = 0
        if pdf_url:
            try:
                pdf_text, pages_extracted = await self._fetch_and_extract_pdf(pdf_url)
            except PDFDownloadError as exc:
                pdf_failure_reason = exc.reason
                logger.warning(
                    "academic_pdf_unavailable",
                    extra={
                        "url": canonical_landing,
                        "pdf_url": pdf_url,
                        "reason": exc.reason,
                        "canonical_id": ref.canonical_id,
                        "cid": request.correlation_id,
                    },
                )
        else:
            pdf_failure_reason = "no_pdf_url_resolved"

        # If the landing page itself shows paywall markers and we have
        # no PDF, surface that distinct reason in the user reply.
        if pdf_text is None and pdf_failure_reason is None:
            pdf_failure_reason = "unknown"
        if pdf_text is None and _landing_looks_like_paywall(landing_markdown):
            pdf_failure_reason = "paywall"

        # 5. Open-access / metadata fallback BEFORE declaring the paper
        # unavailable. When the landing scrape yielded neither an abstract nor a
        # PDF (typical for a Cloudflare-gated SSRN page), recover the abstract
        # and/or an open-access PDF from open scholarly APIs. Off by default and
        # purely additive when on (see app/config/academic.py).
        content_source = "academic_paper_full" if pdf_text else "academic_paper_abstract_only"
        meta_source: str | None = None
        fallback_attempted = self._oa_fallback_enabled(have_content=bool(abstract or pdf_text))
        if fallback_attempted:
            recovery = await self._recover_via_oa_metadata(
                ref, title_hint=title, correlation_id=request.correlation_id
            )
            if recovery is not None:
                if recovery.title:
                    title = recovery.title
                if recovery.abstract:
                    abstract = recovery.abstract
                if recovery.pdf_text:
                    pdf_text = recovery.pdf_text
                    pages_extracted = recovery.pdf_pages
                    pdf_url = recovery.pdf_url
                    pdf_failure_reason = None
                content_source = recovery.content_source
                meta_source = recovery.source

        # 6. Compose content_text ONCE from the now-final title/abstract/body.
        # Order: title → abstract → body (or paywall note). Abstract is always
        # first so that even when the body is truncated the LLM still sees the
        # author-authored TL;DR.
        content_parts: list[str] = []
        if title:
            content_parts.append(f"# {title}")
        if abstract:
            content_parts.append(f"## Abstract\n\n{abstract}")
        if pdf_text:
            content_parts.append(f"## Body\n\n{pdf_text}")
        else:
            content_parts.append(
                f"[PDF unavailable: {pdf_failure_reason} — "
                "summary will be based on the abstract alone.]"
            )
        content_text = "\n\n".join(content_parts)

        # 7. If we STILL have neither abstract nor body, the paper is fully
        # gated and unreachable even via the scholarly APIs. Raise the typed
        # exception so the URL-flow handler shows the accurate 'paper
        # unavailable' message instead of the LLM-parse-error template.
        if not abstract and not pdf_text:
            raise AcademicPaperUnavailableError(
                reason=(
                    "metadata_fallback_exhausted"
                    if fallback_attempted
                    else (pdf_failure_reason or "no_content")
                ),
                host=ref.host.value,
                url=canonical_landing,
            )

        detected_lang = detect_language(content_text)

        if request.mode == "interactive" and request_id is not None:
            await self._lifecycle.persist_detected_lang(request_id, detected_lang)

        return PlatformExtractionResult(
            platform="academic_paper",
            request_id=request_id,
            content_text=content_text,
            content_source=content_source,
            detected_lang=detected_lang,
            title=title,
            metadata={
                "source_type": "academic_paper",
                "host": ref.host.value,
                "paper_canonical_id": ref.canonical_id,
                "paper_id": ref.paper_id,
                "paper_version": ref.version,
                "landing_url": canonical_landing,
                "pdf_url": pdf_url,
                "pdf_extracted": bool(pdf_text),
                "pdf_failure_reason": pdf_failure_reason,
                "pdf_pages_extracted": pages_extracted,
                "abstract_extracted": bool(abstract),
                "metadata_fallback_used": content_source
                in ("academic_metadata_fallback_used", "academic_oa_pdf_used"),
                "metadata_provider": meta_source,
                "oa_pdf_used": content_source == "academic_oa_pdf_used",
                "request_id": request_id,
            },
        )

    # ------------------------------------------------------------------
    # PDF acquisition
    # ------------------------------------------------------------------

    async def _fetch_and_extract_pdf(self, pdf_url: str) -> tuple[str, int]:
        """Download a PDF and return ``(text, page_count)``.

        Raises ``PDFDownloadError`` with a short reason tag on any
        failure mode that the orchestrator can degrade gracefully from.
        """
        try:
            pdf_bytes = await self._download_pdf(pdf_url)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            # 401/402/403 on the PDF endpoint with the patchright cookies
            # already applied at the landing leg almost always means
            # paywall (SSRN, ResearchGate). 404 means the PDF URL rewrite
            # was wrong for this paper id.
            if status in (401, 402, 403):
                raise PDFDownloadError("paywall") from exc
            if status == 404:
                raise PDFDownloadError("pdf_not_found") from exc
            raise PDFDownloadError(f"http_{status}") from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise PDFDownloadError("network_error") from exc

        if not pdf_bytes:
            raise PDFDownloadError("empty_pdf")

        # Detect a non-PDF response that slipped through (some hosts
        # 200-with-HTML when the PDF is gated).
        if not pdf_bytes.lstrip().startswith(b"%PDF"):
            raise PDFDownloadError("not_a_pdf")

        return await asyncio.to_thread(_extract_pdf_text_from_bytes, pdf_bytes)

    async def _download_pdf(self, pdf_url: str) -> bytes:
        """Stream-download a PDF with a hard size cap.

        Raises ``PDFDownloadError`` with a short reason tag on any failure.
        Returns raw PDF bytes without loading the full body into memory in
        one shot — chunks are collected incrementally and the download is
        aborted as soon as the running total exceeds ``self._max_pdf_bytes``.
        """
        _headers = {
            # Several hosts (arXiv, NBER) serve a CDN-cached binary
            # directly; SSRN's Delivery.cfm is more forgiving with a
            # real browser UA.
            "User-Agent": (
                "Mozilla/5.0 (compatible; ratatoskr/1.0; +https://github.com/po4yka/ratatoskr)"
            ),
            "Accept": "application/pdf,*/*;q=0.8",
        }
        if self._http_client_factory is not None:
            client_cm = self._http_client_factory()
        else:
            client_cm = make_safe_async_client(
                timeout=httpx.Timeout(_PDF_DOWNLOAD_TIMEOUT_SEC),
                follow_redirects=False,
                headers=_headers,
            )
        async with client_cm as client:
            current_url = pdf_url
            for _ in range(5):
                safe, reason = is_url_safe(current_url)
                if not safe:
                    raise PDFDownloadError(f"ssrf_blocked:{reason}")
                async with client.stream("GET", current_url, headers=_headers) as resp:
                    if resp.status_code in {301, 302, 303, 307, 308}:
                        location = resp.headers.get("location")
                        await resp.aclose()
                        if not location:
                            resp.raise_for_status()
                        current_url = urljoin(current_url, location)
                        continue
                    resp.raise_for_status()

                    # Reject early if Content-Length already exceeds cap.
                    cl = resp.headers.get("content-length")
                    if cl:
                        try:
                            if int(cl) > self._max_pdf_bytes:
                                raise PDFDownloadError("pdf_too_large")
                        except ValueError:
                            pass

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        total += len(chunk)
                        if total > self._max_pdf_bytes:
                            raise PDFDownloadError("pdf_too_large")
                        chunks.append(chunk)
                    return b"".join(chunks)
            raise PDFDownloadError("too_many_redirects")

    # ------------------------------------------------------------------
    # Open-access / metadata fallback
    # ------------------------------------------------------------------

    def _oa_fallback_enabled(self, *, have_content: bool) -> bool:
        """Whether the OA/metadata fallback should run for this (gated) request."""
        if have_content:
            return False
        cfg = getattr(self._cfg, "academic", None)
        return bool(cfg is not None and cfg.metadata_fallback_enabled and cfg.contact_email)

    async def _recover_via_oa_metadata(
        self,
        ref: AcademicPaperRef,
        *,
        title_hint: str | None,
        correlation_id: str | None,
    ) -> _OARecovery | None:
        """Recover an abstract / open-access PDF from open scholarly APIs.

        Called only when the landing scrape yielded neither abstract nor PDF and
        the fallback is enabled with a contact email. Best-effort: returns
        ``None`` when nothing usable was recovered. An OA PDF (a full body) is
        preferred over an abstract and is run back through the existing hardened
        ``_fetch_and_extract_pdf`` so it inherits the SSRF / size / magic-byte
        protections.
        """
        academic_cfg = self._cfg.academic
        oa = await fetch_oa_metadata(
            ref,
            contact_email=academic_cfg.contact_email,
            title_hint=(None if _is_synthetic_title(title_hint, ref) else title_hint),
            timeout_sec=academic_cfg.api_timeout_sec,
            http_client_factory=self._http_client_factory,
            correlation_id=correlation_id,
        )
        if oa is None:
            return None

        pdf_text: str | None = None
        pdf_pages = 0
        pdf_url: str | None = None
        content_source = "academic_metadata_fallback_used"

        if oa.oa_pdf_url:
            try:
                pdf_text, pdf_pages = await self._fetch_and_extract_pdf(oa.oa_pdf_url)
                pdf_url = oa.oa_pdf_url
                content_source = "academic_oa_pdf_used"
            except PDFDownloadError as exc:
                logger.info(
                    "academic_oa_pdf_unavailable",
                    extra={
                        "canonical_id": ref.canonical_id,
                        "reason": f"oa_{exc.reason}",
                        "cid": correlation_id,
                    },
                )

        abstract = (oa.abstract or oa.tldr) if pdf_text is None else None
        new_title = oa.title if (oa.title and _is_synthetic_title(title_hint, ref)) else None

        if not pdf_text and not abstract:
            return None

        logger.info(
            "academic_metadata_fallback_used",
            extra={
                "canonical_id": ref.canonical_id,
                "source": oa.source,
                "used_oa_pdf": content_source == "academic_oa_pdf_used",
                "cid": correlation_id,
            },
        )
        return _OARecovery(
            title=new_title,
            abstract=abstract,
            pdf_text=pdf_text,
            pdf_pages=pdf_pages,
            pdf_url=pdf_url,
            content_source=content_source,
            source=oa.source,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_markdown(crawl: Any) -> str:
        if crawl is None:
            return ""
        if getattr(crawl, "status", None) != CallStatus.OK:
            # Even on chain failure the crawl_result may carry partial
            # markdown (e.g. a stub abstract from a quick provider) —
            # use it if present.
            return str(getattr(crawl, "content_markdown", "") or "")
        return str(getattr(crawl, "content_markdown", "") or "")


# ---------------------------------------------------------------------------
# Module-level helpers — kept as free functions so they can be unit-tested
# without spinning up the orchestrator.
# ---------------------------------------------------------------------------


_ABSTRACT_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:abstract|summary)\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_NEXT_HEADING_RE = re.compile(r"^\s*#{1,6}\s+\S", re.MULTILINE)
_PDF_ANCHOR_RE = re.compile(
    r"\[(?:[^\]]*)\]\((?P<url>https?://[^)\s]+\.pdf(?:\?[^)\s]*)?)\)",
    re.IGNORECASE,
)


def _extract_abstract(markdown: str) -> str | None:
    """Heuristic: take the text under the first "Abstract" heading.

    Works across hosts because the scraper chain normalizes their
    landing HTML to markdown where the abstract is consistently under
    an ``## Abstract`` heading or labelled paragraph. Returns None if
    no abstract section is identifiable.
    """
    if not markdown:
        return None
    heading_match = _ABSTRACT_HEADING_RE.search(markdown)
    if heading_match is None:
        return None
    start = heading_match.end()
    next_heading = _NEXT_HEADING_RE.search(markdown, pos=start)
    end = next_heading.start() if next_heading else len(markdown)
    abstract = markdown[start:end].strip()
    return abstract or None


def _extract_title(
    ref: AcademicPaperRef,
    markdown: str,
    metadata_json: dict[str, Any],
) -> str | None:
    """Title comes from (in order): scraper metadata → first H1 → host fallback."""
    for key in ("title", "og:title", "headline", "citation_title"):
        value = metadata_json.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    h1 = re.search(r"^\s*#\s+(.+)$", markdown, re.MULTILINE)
    if h1:
        return h1.group(1).strip() or None
    # Generic fallback so the user-facing reply isn't empty.
    return f"{ref.host.value.upper()} paper {ref.paper_id}"


def _is_synthetic_title(title: str | None, ref: AcademicPaperRef) -> bool:
    """True when ``title`` is the host-fallback placeholder, not a real title.

    ``_extract_title`` returns ``"{HOST} paper {id}"`` when no real title was
    harvested; that must never be used as a title-search query (it would poison
    the OpenAlex / Semantic Scholar title match) nor be overwritten in place.
    """
    return (not title) or title == f"{ref.host.value.upper()} paper {ref.paper_id}"


def _harvest_pdf_anchor(markdown: str) -> str | None:
    """Find the first markdown link whose URL ends in ``.pdf``.

    Used only for hosts where ``pdf_url_for`` returns None
    (ResearchGate, RePEc). Picks the first match; landing pages
    usually link to the canonical PDF before any supplementary
    materials.
    """
    if not markdown:
        return None
    match = _PDF_ANCHOR_RE.search(markdown)
    return match.group("url") if match else None


def _landing_looks_like_paywall(markdown: str) -> bool:
    if not markdown:
        return False
    lowered = markdown.lower()
    return any(marker in lowered for marker in _PAYWALL_HTML_MARKERS)


def _extract_pdf_text_from_bytes(pdf_bytes: bytes) -> tuple[str, int]:
    """Extract text from PDF bytes via pymupdf (no tempfile).

    Returns ``(body_text, page_count)``. Text-only extraction (no
    page rendering, no embedded-image extraction) so we don't drag
    in Pillow / the full ``attachment`` extra — the academic path
    feeds a text LLM, not a vision model. The page text uses block
    ordering for reading-order stability.
    """
    try:
        import fitz  # PyMuPDF
    except ModuleNotFoundError as exc:
        raise PDFDownloadError("pymupdf_missing") from exc

    try:
        doc = fitz.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
    except Exception as exc:
        raise PDFDownloadError("pdf_parse_failed") from exc

    try:
        if doc.is_encrypted:
            raise PDFDownloadError("pdf_encrypted")
        total_pages = len(doc)
        pages_to_process = min(total_pages, _DEFAULT_PDF_MAX_PAGES)
        text_parts: list[str] = []
        for page_idx in range(pages_to_process):
            page = doc[page_idx]
            blocks = page.get_text("blocks")
            blocks.sort(key=lambda b: (b[1], b[0]))
            page_text = "\n".join(b[4].strip() for b in blocks if b[4].strip())
            if page_text:
                text_parts.append(f"--- Page {page_idx + 1} ---\n{page_text}")
        full_text = "\n\n".join(text_parts)
        return full_text, total_pages
    finally:
        doc.close()


__all__ = [
    "AcademicPaperUnavailableError",
    "AcademicPlatformExtractor",
    "PDFDownloadError",
]
