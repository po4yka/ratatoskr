"""Open scholarly-metadata + open-access fallback for academic papers.

When the academic scraper can't reach a paper's landing page (Cloudflare-gated
SSRN / ResearchGate / ...), this module recovers the paper's abstract and/or an
open-access PDF over open, un-gated scholarly APIs:

  * OpenAlex          -> title + abstract (rebuilt from its inverted index) + a
                         free open-access PDF cross-check
  * Semantic Scholar  -> abstract + a one-line TLDR
  * Crossref          -> title + (JATS-XML) abstract
  * Unpaywall         -> a free, legal open-access PDF URL for the DOI

Every network call is best-effort: a provider failure (404, 429, timeout, SSRF
block, parse error) is swallowed and the chain falls through. ``fetch_oa_metadata``
returns ``None`` (never raises) when nothing usable was recovered, so the caller
can still raise its honest "paper unavailable" error.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from app.adapters.academic.url_patterns import AcademicHost, AcademicPaperRef
from app.core.logging_utils import get_logger
from app.security.ssrf import is_url_safe_async, make_safe_async_client

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_OPENALEX = "https://api.openalex.org/works"
_SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper"
_CROSSREF = "https://api.crossref.org/works"
_UNPAYWALL = "https://api.unpaywall.org/v2"

# Metadata endpoints are fast; a short connect timeout just speeds the
# fall-through to the next provider when one is unreachable.
_CONNECT_TIMEOUT_SEC = 5.0


@dataclass(frozen=True)
class OAMetadata:
    """What the open-access / metadata chain recovered for one paper."""

    title: str | None
    abstract: str | None
    tldr: str | None
    doi: str | None
    oa_pdf_url: str | None
    source: str | None  # provider that supplied the abstract (telemetry)


# ---------------------------------------------------------------------------
# Pure helpers (no IO) -- unit-testable without a network.
# ---------------------------------------------------------------------------


def doi_for(ref: AcademicPaperRef) -> str | None:
    """Deterministic DOI per host, or ``None`` when only a title search is possible.

    The version is intentionally dropped: there is one DOI per preprint, not one
    per version. Old-style arXiv ids keep their internal slash
    (``10.48550/arXiv.hep-th/0102003``).
    """
    pid = ref.paper_id
    if ref.host == AcademicHost.ARXIV:
        return f"10.48550/arXiv.{pid}"
    if ref.host == AcademicHost.SSRN:
        return f"10.2139/ssrn.{pid}"
    if ref.host == AcademicHost.NBER:
        # The suffix includes the leading working-paper letter (w/t/h) already.
        return f"10.3386/{pid}"
    if ref.host == AcademicHost.OSF:
        # Best-effort: OSF-hosted preprints use 10.31219; branded servers
        # (PsyArXiv 10.31234, SocArXiv 10.31235) differ and fall through to
        # the title search when this guess 404s.
        return f"10.31219/osf.io/{pid}"
    return None


def reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str | None:
    """Rebuild a plain abstract from OpenAlex's ``{word: [positions]}`` index.

    OpenAlex serves no plain-text abstract -- only this inverted index -- so the
    words must be re-ordered by position and space-joined.
    """
    if not inverted_index:
        return None
    positioned: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        # Defensive against malformed API data: positions must be a list of
        # ints, else the sort below would raise out of the never-raise chain.
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int):
                positioned.append((pos, word))
    if not positioned:
        return None
    positioned.sort(key=lambda pair: pair[0])
    text = " ".join(word for _, word in positioned).strip()
    return text or None


_JATS_TITLE_RE = re.compile(r"<jats:title>.*?</jats:title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_jats(abstract: str | None) -> str | None:
    """Reduce a Crossref JATS-XML abstract to plain text."""
    if not isinstance(abstract, str) or not abstract:
        return None
    cleaned = _JATS_TITLE_RE.sub("", abstract)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    return cleaned or None


def _http_url(value: Any) -> str | None:
    """Return ``value`` only if it is an ``http(s)`` URL.

    A provider response is untrusted input; an ``oa_pdf_url`` is later fetched,
    so reject ``file:`` / ``data:`` / other schemes here (defense-in-depth on top
    of the downstream ``is_url_safe`` + safe-client checks).
    """
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def best_oa_pdf(unpaywall_json: dict[str, Any] | None) -> str | None:
    """Pick the best open-access PDF URL (http/https only) from Unpaywall."""
    if not unpaywall_json or not unpaywall_json.get("is_oa"):
        return None
    best = unpaywall_json.get("best_oa_location") or {}
    url = _http_url(best.get("url_for_pdf")) or _http_url(best.get("url"))
    if url:
        return url
    for loc in unpaywall_json.get("oa_locations") or []:
        if isinstance(loc, dict):
            candidate = _http_url(loc.get("url_for_pdf"))
            if candidate:
                return candidate
    return None


def title_matches(known: str | None, candidate: str | None, *, threshold: float = 0.9) -> bool:
    """Fuzzy-verify a title-search hit so a near-miss paper is not trusted."""
    if not isinstance(known, str) or not isinstance(candidate, str):
        return False
    a = _WS_RE.sub(" ", known).strip().lower()
    b = _WS_RE.sub(" ", candidate).strip().lower()
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold


def _openalex_oa_pdf(work: dict[str, Any]) -> str | None:
    best = work.get("best_oa_location") or {}
    candidate = _http_url(best.get("pdf_url"))
    if candidate:
        return candidate
    oa = work.get("open_access") or {}
    return _http_url(oa.get("oa_url"))


# ---------------------------------------------------------------------------
# Network chain -- best-effort, never raises.
# ---------------------------------------------------------------------------


async def _safe_get_json(
    client: Any,
    url: str,
    params: dict[str, Any] | None,
    provider: str,
    correlation_id: str | None,
) -> Any | None:
    """SSRF-guarded GET returning parsed JSON, or ``None`` on any failure."""
    try:
        safe, reason = await is_url_safe_async(url)
        if not safe:
            logger.info(
                "academic_oa_provider_ssrf_blocked",
                extra={"provider": provider, "reason": reason, "cid": correlation_id},
            )
            return None
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as exc:  # best-effort: a provider hiccup must never propagate
        logger.info(
            "academic_oa_provider_error",
            extra={"provider": provider, "error": str(exc), "cid": correlation_id},
        )
        return None


async def _try_openalex(
    client: Any,
    doi: str | None,
    title_hint: str | None,
    email: str,
    correlation_id: str | None,
) -> dict[str, Any] | None:
    if doi is not None:
        url = f"{_OPENALEX}/doi:{quote(doi, safe='/')}"
        params: dict[str, Any] = {"mailto": email}
    elif title_hint:
        url = _OPENALEX
        params = {"filter": f"title.search:{title_hint}", "per_page": "1", "mailto": email}
    else:
        return None
    data = await _safe_get_json(client, url, params, "openalex", correlation_id)
    if not isinstance(data, dict):
        return None
    if "results" in data:  # title-search response wraps the works in a list
        results = data.get("results") or []
        first = results[0] if results else None
        return first if isinstance(first, dict) else None
    return data if data.get("id") else None


async def _try_semantic_scholar(
    client: Any,
    ref: AcademicPaperRef,
    doi: str | None,
    correlation_id: str | None,
) -> dict[str, Any] | None:
    if ref.host == AcademicHost.ARXIV:
        ident = f"ARXIV:{ref.paper_id}"
    elif doi is not None:
        ident = f"DOI:{doi}"
    else:
        return None
    url = f"{_SEMANTIC_SCHOLAR}/{quote(ident, safe=':/')}"
    params = {"fields": "title,abstract,tldr"}
    data = await _safe_get_json(client, url, params, "semantic_scholar", correlation_id)
    return data if isinstance(data, dict) else None


async def _try_crossref(
    client: Any,
    doi: str,
    correlation_id: str | None,
) -> dict[str, Any] | None:
    url = f"{_CROSSREF}/{quote(doi, safe='/')}"
    data = await _safe_get_json(client, url, None, "crossref", correlation_id)
    if isinstance(data, dict) and isinstance(data.get("message"), dict):
        return data["message"]
    return None


async def _try_unpaywall(
    client: Any,
    doi: str,
    email: str,
    correlation_id: str | None,
) -> dict[str, Any] | None:
    url = f"{_UNPAYWALL}/{quote(doi, safe='/')}"
    data = await _safe_get_json(client, url, {"email": email}, "unpaywall", correlation_id)
    return data if isinstance(data, dict) else None


async def fetch_oa_metadata(
    ref: AcademicPaperRef,
    *,
    contact_email: str,
    title_hint: str | None = None,
    timeout_sec: float = 12.0,
    http_client_factory: Callable[[], Any] | None = None,
    correlation_id: str | None = None,
) -> OAMetadata | None:
    """Recover a paper's abstract / open-access PDF from open scholarly APIs.

    Sequential, early-exit on the first non-empty abstract; Semantic Scholar's
    TLDR is captured opportunistically even when OpenAlex already supplied the
    abstract. Best-effort: returns ``None`` (never raises) when nothing usable
    is found, including the ``title_hint``-only case where no provider verifies.
    """
    doi = doi_for(ref)
    headers = {
        "User-Agent": (
            f"ratatoskr/1.0 (+https://github.com/po4yka/ratatoskr; mailto:{contact_email})"
        ),
        "Accept": "application/json",
    }
    timeout = httpx.Timeout(timeout_sec, connect=_CONNECT_TIMEOUT_SEC)

    if http_client_factory is not None:
        client_cm = http_client_factory()
    else:
        client_cm = make_safe_async_client(timeout=timeout, follow_redirects=True, headers=headers)

    title: str | None = None
    abstract: str | None = None
    tldr: str | None = None
    oa_pdf_url: str | None = None
    source: str | None = None

    async with client_cm as client:
        # 1. OpenAlex (primary).
        work = await _try_openalex(client, doi, title_hint, contact_email, correlation_id)
        if work is not None:
            cand_title = work.get("title")
            # A title-search hit (no DOI) must fuzzy-match the landing title.
            if doi is not None or title_matches(title_hint, cand_title):
                title = title or cand_title
                cand_abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
                if cand_abstract:
                    abstract = cand_abstract
                    source = "openalex"
                oa_pdf_url = oa_pdf_url or _openalex_oa_pdf(work)

        # 2. Semantic Scholar (secondary) -- abstract if still missing, TLDR always.
        s2 = await _try_semantic_scholar(client, ref, doi, correlation_id)
        if s2 is not None:
            title = title or s2.get("title")
            if not abstract and s2.get("abstract"):
                abstract = s2["abstract"]
                source = source or "semantic_scholar"
            s2_tldr = s2.get("tldr")
            if isinstance(s2_tldr, dict) and s2_tldr.get("text"):
                tldr = str(s2_tldr["text"])

        # 3. Crossref (tertiary) -- only when an abstract is still missing.
        if not abstract and doi is not None:
            message = await _try_crossref(client, doi, correlation_id)
            if message is not None:
                if not title:
                    titles = message.get("title") or []
                    title = titles[0] if titles else None
                cr_abstract = strip_jats(message.get("abstract"))
                if cr_abstract:
                    abstract = cr_abstract
                    source = source or "crossref"

        # 4. Unpaywall (open-access full text) -- when OpenAlex found no OA PDF.
        if doi is not None and oa_pdf_url is None:
            unpaywall = await _try_unpaywall(client, doi, contact_email, correlation_id)
            oa_pdf_url = best_oa_pdf(unpaywall)

    if not abstract and not tldr and not oa_pdf_url:
        return None
    return OAMetadata(
        title=title,
        abstract=abstract,
        tldr=tldr,
        doi=doi,
        oa_pdf_url=oa_pdf_url,
        source=source,
    )
