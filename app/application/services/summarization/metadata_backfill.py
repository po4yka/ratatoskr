"""Summary metadata backfill for the summarize graph (GAP 4).

Ports the non-LLM subset of
:meth:`~app.adapters.content.llm_summarizer_metadata.LLMSummaryMetadataHelper.ensure_summary_metadata`
into the application layer so the ``persist`` node can backfill missing metadata
fields without importing ``app.adapters`` (``application-no-outward``).

**Parity vs. legacy:** covers all crawl/request/URL-derived backfill steps:

1. Firecrawl ``metadata_json`` -> ``title``, ``canonical_url``, ``author``,
   ``published_at``, ``last_updated`` (via the same alias map).
2. Request ``normalized_url`` -> ``canonical_url``.
3. ``canonical_url`` or request URL -> ``domain`` (via ``app.core.url_utils``).
4. Content heading heuristic -> ``title``.

**Deviation from legacy (documented):** the optional LLM metadata-completion call
(``_generate_metadata_completion``) and ``_semantic_helper.enrich_with_rag_fields``
are NOT reproduced here.  The LLM completion runs in the adapter tier using the
raw OpenRouter client (not the port); porting it would require either a new port
method or an adapter import -- both violate the layer boundary.
``enrich_with_rag_fields`` is a RAG-optimisation step that enriches already-present
metadata, not a gap closer.  Both can be added in a follow-up once the port surface
is expanded.  The most common real-world gap -- ``canonical_url`` / ``domain`` /
``title`` from the page scrape -- is covered.

Only ``app.core`` and ``app.application.ports.*`` are imported at module scope
(legal from the application layer).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger
from app.core.url_utils import extract_domain

if TYPE_CHECKING:
    from app.application.ports.requests import CrawlResultRepositoryPort, RequestRepositoryPort

logger = get_logger(__name__)

_METADATA_FIELDS: tuple[str, ...] = (
    "title",
    "canonical_url",
    "domain",
    "author",
    "published_at",
    "last_updated",
)

# Alias map verbatim from LLMSummaryMetadataHelper._FIRECRAWL_FIELD_ALIASES.
_FIRECRAWL_ALIASES: dict[str, tuple[str, ...]] = {
    "title": (
        "title",
        "og:title",
        "og_title",
        "meta_title",
        "twitter:title",
        "headline",
        "dc.title",
        "article:title",
    ),
    "canonical_url": (
        "canonical",
        "canonical_url",
        "og:url",
        "og_url",
        "url",
    ),
    "author": (
        "author",
        "article:author",
        "byline",
        "twitter:creator",
        "dc.creator",
        "creator",
    ),
    "published_at": (
        "article:published_time",
        "article:published",
        "article:publish_time",
        "article:publish_date",
        "datepublished",
        "date_published",
        "publish_date",
        "published",
        "pubdate",
    ),
    "last_updated": (
        "article:modified_time",
        "article:updated_time",
        "date_modified",
        "datemodified",
        "updated",
        "lastmod",
        "last_modified",
    ),
}


async def backfill_summary_metadata(
    summary: dict[str, Any],
    *,
    request_id: int,
    content_text: str,
    correlation_id: str | None,
    request_repo: RequestRepositoryPort,
    crawl_repo: CrawlResultRepositoryPort,
) -> dict[str, Any]:
    """Backfill missing ``metadata`` fields in-place and return ``summary``.

    Mirrors steps 1-4 of
    :meth:`~app.adapters.content.llm_summarizer_metadata.LLMSummaryMetadataHelper.ensure_summary_metadata`
    without the LLM-completion and RAG-enrichment steps (see module docstring).
    Best-effort: any DB failure is logged and skipped; the summary is never blocked.
    """
    metadata = summary.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        summary["metadata"] = metadata

    missing: set[str] = {f for f in _METADATA_FIELDS if _is_blank(metadata.get(f))}
    if not missing:
        return summary

    # Step 1: firecrawl metadata_json
    try:
        crawl_row = await crawl_repo.async_get_crawl_result_by_request(request_id)
        if crawl_row:
            flat = _flatten_crawl_metadata(crawl_row)
            if flat:
                filled = _apply_aliases(metadata, missing, flat, correlation_id)
                missing -= filled
    except Exception as exc:
        logger.warning(
            "metadata_backfill_crawl_lookup_failed",
            extra={"cid": correlation_id, "request_id": request_id, "error": str(exc)},
        )

    if not missing:
        return summary

    # Step 2: request URL -> canonical_url
    request_url: str | None = None
    try:
        request_row = await request_repo.async_get_request_by_id(request_id)
        if request_row:
            candidate = request_row.get("normalized_url") or request_row.get("input_url")
            if isinstance(candidate, str) and candidate.strip():
                request_url = candidate.strip()
    except Exception as exc:
        logger.warning(
            "metadata_backfill_request_lookup_failed",
            extra={"cid": correlation_id, "request_id": request_id, "error": str(exc)},
        )

    if "canonical_url" in missing and request_url:
        metadata["canonical_url"] = request_url
        missing.discard("canonical_url")
        logger.debug(
            "metadata_backfill",
            extra={"cid": correlation_id, "field": "canonical_url", "source": "request"},
        )

    # Step 3: domain from canonical_url or request_url
    if _is_blank(metadata.get("domain")):
        domain_src = metadata.get("canonical_url") or request_url
        domain_val = extract_domain(domain_src) if domain_src else None
        if domain_val:
            metadata["domain"] = domain_val
            missing.discard("domain")
            logger.debug(
                "metadata_backfill",
                extra={"cid": correlation_id, "field": "domain", "source": "url"},
            )

    # Step 4: heading heuristic -> title
    if "title" in missing:
        heading = _extract_heading_title(content_text)
        if heading:
            metadata["title"] = heading
            missing.discard("title")
            logger.debug(
                "metadata_backfill",
                extra={"cid": correlation_id, "field": "title", "source": "heading"},
            )

    if missing:
        logger.info(
            "metadata_fields_still_missing",
            extra={"cid": correlation_id, "fields": sorted(missing)},
        )

    return summary


# ---------------------------------------------------------------------------
# Internal helpers (verbatim logic from LLMSummaryMetadataHelper)
# ---------------------------------------------------------------------------


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return not str(value).strip()


def _apply_aliases(
    metadata: dict[str, Any],
    missing: set[str],
    flat: dict[str, str],
    correlation_id: str | None,
) -> set[str]:
    """Apply firecrawl alias values; return the set of newly-filled fields."""
    filled: set[str] = set()
    for field in list(missing):
        for alias in _FIRECRAWL_ALIASES.get(field, ()):
            candidate = flat.get(alias)
            if _is_blank(candidate):
                continue
            metadata[field] = str(candidate).strip()
            filled.add(field)
            logger.debug(
                "metadata_backfill",
                extra={"cid": correlation_id, "field": field, "source": f"firecrawl:{alias}"},
            )
            break
    return filled


def _flatten_crawl_metadata(crawl_row: dict[str, Any]) -> dict[str, str]:
    """Flatten crawl row metadata_json (or raw_response_json) into a single dict."""
    parsed: Any = None
    metadata_raw = crawl_row.get("metadata_json")
    if metadata_raw:
        if isinstance(metadata_raw, dict):
            parsed = metadata_raw
        else:
            try:
                parsed = json.loads(metadata_raw)
            except Exception:
                pass

    if parsed is None:
        raw_payload = crawl_row.get("raw_response_json")
        if raw_payload:
            try:
                payload = raw_payload if isinstance(raw_payload, dict) else json.loads(raw_payload)
                if isinstance(payload, dict):
                    data_block = payload.get("data")
                    if isinstance(data_block, dict):
                        parsed = data_block.get("metadata") or data_block.get("meta")
            except Exception:
                pass

    if parsed is None:
        return {}

    flat: dict[str, str] = {}
    _flatten_node(parsed, flat)
    return flat


def _flatten_node(node: Any, collector: dict[str, str]) -> None:
    """Recursively flatten metadata values (verbatim from LLMSummaryMetadataHelper)."""
    if node is None or isinstance(node, str | int | float):
        return
    if isinstance(node, dict):
        key_hint: str | None = None
        for hint_key in ("property", "name", "itemprop", "rel", "key", "type"):
            if hint_key in node and isinstance(node[hint_key], str | int | float):
                candidate = str(node[hint_key]).strip().lower()
                if candidate:
                    key_hint = candidate
                    break
        value_hint = node.get("content") or node.get("value") or node.get("text")
        if key_hint and isinstance(value_hint, str | int | float):
            cleaned = str(value_hint).strip()
            if cleaned and key_hint not in collector:
                collector[key_hint] = cleaned
        for key, value in node.items():
            norm_key = str(key).strip().lower()
            if isinstance(value, str | int | float):
                cleaned_child = str(value).strip()
                if cleaned_child and norm_key:
                    collector.setdefault(norm_key, cleaned_child)
            else:
                _flatten_node(value, collector)
        return
    if isinstance(node, list):
        for item in node:
            _flatten_node(item, collector)


def _extract_heading_title(content_text: str) -> str | None:
    """Derive a title from the first markdown heading or leading line (verbatim)."""
    if not content_text:
        return None
    match = re.search(r"^#{1,6}\s+(.+)$", content_text, flags=re.MULTILINE)
    if match:
        candidate = match.group(1).strip(" #\t")
        if candidate:
            return candidate
    lines = [line.strip() for line in content_text.splitlines() if line.strip()]
    if not lines:
        return None
    preamble_pattern = re.compile(r"^\[source:.*\]$", flags=re.IGNORECASE)
    metadata_prefixes = ("channel:", "duration:", "resolution:")
    for line in lines:
        lower = line.lower()
        if lower.startswith("title:"):
            title_part = line.split("|", 1)[0]
            candidate = title_part.split(":", 1)[1].strip()
            if candidate:
                return candidate
            continue
        if preamble_pattern.match(line):
            continue
        if lower.startswith(metadata_prefixes):
            continue
        if len(line) <= 140:
            return line
    return None
