"""Utilities for selecting article image candidates for multimodal summarization."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from app.application.dto.aggregation import SourceMediaAsset, SourceMediaKind

if TYPE_CHECKING:
    from app.adapters.external.firecrawl.models import FirecrawlResult

ImageRole = Literal["header_og", "content_area", "thumbnail", "unknown"]

_OG_SOURCE_KEYS = frozenset({"og:image", "ogImage", "image", "image_url"})
_CONTENT_SOURCE_KEYS = frozenset({"markdown_image", "images", "image_urls"})
_THUMBNAIL_SOURCE_KEYS = frozenset({"thumbnails", "screenshots"})
_THUMBNAIL_MIN_WIDTH = 200
_THUMBNAIL_MIN_HEIGHT = 150

_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^\s\)]+)\)")
_DECORATIVE_PATH_TERMS = (
    "logo",
    "icon",
    "favicon",
    "avatar",
    "sprite",
    "placeholder",
    "pixel",
    "tracker",
    "tracking",
    "badge",
    "emoji",
)
_DECORATIVE_ALT_TERMS = (
    "logo",
    "icon",
    "avatar",
    "profile picture",
    "tracking pixel",
    "decorative",
)
_TRACKING_HOST_TERMS = (
    "doubleclick",
    "google-analytics",
    "facebook.com/tr",
    "facebook.net",
    "googletagmanager",
)
_BLOCKED_EXTENSIONS = (".svg", ".ico")
# Path segments that indicate an unsubstituted JS template variable (e.g. Wired's
# Cloudinary URLs sometimes serialize with "/undefined" when the filename
# placeholder did not render). Such URLs 404 when fetched downstream.
_INVALID_PATH_SEGMENTS = ("/undefined", "/null", "/none", "/[object%20object]")


def extract_firecrawl_image_assets(
    crawl: FirecrawlResult | Any,
    *,
    max_assets: int = 5,
    role_filter_enabled: bool = True,
) -> tuple[list[SourceMediaAsset], dict[str, Any]]:
    """Extract and quality-filter image candidates from Firecrawl output.

    When ``role_filter_enabled`` is True (the default), decorative header images
    (``og:image``/``ogImage``) and small thumbnails are dropped whenever at least
    one content-area image survives quality filtering. If no content-area image
    is available, decorative candidates are kept so the article still has a
    chance at vision routing. The classification is also stamped into each
    asset's ``metadata['role']`` and summarized in ``report['role_breakdown']``
    for observability downstream.
    """

    candidates = _collect_firecrawl_image_candidates(crawl)
    rejected_counts: dict[str, int] = {}
    best_by_url: dict[str, SourceMediaAsset] = {}

    for candidate in candidates:
        url = str(candidate.get("url") or "").strip()
        if not url:
            _increment(rejected_counts, "missing_url")
            continue
        allowed, reason = _is_content_image_candidate(candidate)
        if not allowed:
            _increment(rejected_counts, reason)
            continue

        role = _classify_image_role(candidate)
        asset = SourceMediaAsset(
            kind=SourceMediaKind.IMAGE,
            url=url,
            alt_text=_clean_text(candidate.get("alt_text")),
            mime_type=_clean_text(candidate.get("mime_type")),
            metadata={
                "source": candidate.get("source"),
                "source_key": candidate.get("source_key"),
                "width": candidate.get("width"),
                "height": candidate.get("height"),
                "role": role,
            },
        )
        existing = best_by_url.get(url)
        if existing is None or (not existing.alt_text and asset.alt_text):
            best_by_url[url] = asset

    quality_filtered = list(best_by_url.values())
    role_breakdown = _summarize_roles(quality_filtered)

    role_filter_applied = False
    role_filtered: list[SourceMediaAsset]
    if role_filter_enabled and role_breakdown.get("content_area", 0) > 0:
        role_filtered = [
            asset
            for asset in quality_filtered
            if asset.metadata.get("role") in ("content_area", "unknown")
        ]
        dropped = len(quality_filtered) - len(role_filtered)
        if dropped > 0:
            role_filter_applied = True
            rejected_counts["role_filter_decorative"] = dropped
    else:
        role_filtered = quality_filtered

    selected: list[SourceMediaAsset] = []
    for asset in role_filtered:
        if len(selected) >= max_assets:
            _increment(rejected_counts, "max_assets")
            continue
        selected.append(asset.model_copy(update={"position": len(selected)}))

    report = {
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "rejected_count": max(0, len(candidates) - len(selected)),
        "rejected_reasons": rejected_counts,
        "strategy": "firecrawl_metadata_and_markdown",
        "role_breakdown": role_breakdown,
        "role_filter_applied": role_filter_applied,
        "role_filter_enabled": role_filter_enabled,
    }
    return selected, report


def _classify_image_role(candidate: dict[str, Any]) -> ImageRole:
    """Classify an image candidate's role in the article layout."""
    source_key = (candidate.get("source_key") or "").strip()
    width = _coerce_int(candidate.get("width"))
    height = _coerce_int(candidate.get("height"))

    if source_key in _THUMBNAIL_SOURCE_KEYS:
        return "thumbnail"
    if (
        width is not None
        and height is not None
        and width < _THUMBNAIL_MIN_WIDTH
        and height < _THUMBNAIL_MIN_HEIGHT
    ):
        return "thumbnail"
    if source_key in _OG_SOURCE_KEYS:
        return "header_og"
    if source_key in _CONTENT_SOURCE_KEYS:
        return "content_area"
    return "unknown"


def _summarize_roles(assets: list[SourceMediaAsset]) -> dict[str, int]:
    """Return a count-by-role breakdown for the given assets."""
    breakdown: dict[str, int] = {
        "header_og": 0,
        "content_area": 0,
        "thumbnail": 0,
        "unknown": 0,
    }
    for asset in assets:
        role = asset.metadata.get("role")
        if role in breakdown:
            breakdown[role] += 1
        else:
            breakdown["unknown"] += 1
    return breakdown


def _collect_firecrawl_image_candidates(crawl: FirecrawlResult | Any) -> list[dict[str, Any]]:
    raw_metadata = getattr(crawl, "metadata_json", None)
    metadata_json = raw_metadata if isinstance(raw_metadata, dict) else {}
    candidates: list[dict[str, Any]] = []

    for key in ("images", "image_urls", "thumbnails", "screenshots"):
        candidates.extend(_coerce_candidates(metadata_json.get(key), source_key=key))
    for key in ("image", "image_url", "og:image", "ogImage"):
        candidates.extend(_coerce_candidates(metadata_json.get(key), source_key=key))

    content_markdown = getattr(crawl, "content_markdown", None)
    if content_markdown:
        for alt_text, url in _MARKDOWN_IMAGE_RE.findall(content_markdown):
            candidates.append(
                {
                    "url": url.strip(),
                    "alt_text": alt_text.strip() or None,
                    "source": "markdown",
                    "source_key": "markdown_image",
                }
            )

    return candidates


def _coerce_candidates(value: Any, *, source_key: str) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else [value]
    candidates: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            candidates.append(
                {
                    "url": item.strip(),
                    "source": "firecrawl_metadata",
                    "source_key": source_key,
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        candidates.append(
            {
                "url": _clean_text(
                    item.get("url") or item.get("src") or item.get("image") or item.get("image_url")
                ),
                "alt_text": _clean_text(item.get("alt") or item.get("alt_text")),
                "mime_type": _clean_text(item.get("mime_type") or item.get("content_type")),
                "width": _coerce_int(item.get("width")),
                "height": _coerce_int(item.get("height")),
                "source": "firecrawl_metadata",
                "source_key": source_key,
            }
        )
    return candidates


def _is_content_image_candidate(candidate: dict[str, Any]) -> tuple[bool, str]:
    url = str(candidate.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, "non_http"

    lower_url = url.lower()
    if any(term in lower_url for term in _TRACKING_HOST_TERMS):
        return False, "tracking_host"
    if parsed.path.lower().endswith(_BLOCKED_EXTENSIONS):
        return False, "blocked_extension"

    lower_path = parsed.path.lower()
    if any(segment in lower_path for segment in _INVALID_PATH_SEGMENTS) or lower_path in (
        "/undefined",
        "/null",
    ):
        return False, "unresolved_template"

    path_and_query = f"{parsed.path}?{parsed.query}".lower()
    if any(term in path_and_query for term in _DECORATIVE_PATH_TERMS):
        return False, "decorative_path"

    alt_text = _clean_text(candidate.get("alt_text"))
    if alt_text and any(term in alt_text.lower() for term in _DECORATIVE_ALT_TERMS):
        return False, "decorative_alt"

    width = _coerce_int(candidate.get("width"))
    height = _coerce_int(candidate.get("height"))
    if width is not None and height is not None and width <= 96 and height <= 96:
        return False, "tiny_dimensions"

    return True, "ok"


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
