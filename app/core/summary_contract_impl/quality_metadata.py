"""Helpers for safe, persisted summary quality metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.summary_contract_impl.common import SummaryJSON

SUMMARY_QUALITY_KEY = "summary_quality"

VALID_SOURCE_COVERAGE = {
    "full",
    "partial",
    "abstract_only",
    "transcript_missing",
    "unknown",
}

_FULL_SOURCE_MARKERS = {
    "markdown",
    "html",
    "firecrawl",
    "crawl4ai",
    "direct_html",
    "github_api",
    "twitter_graphql",
    "twitter_article",
    "youtube-transcript-api",
    "vtt",
    "academic_paper_full",
}


def _clean_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_warning(value: Any) -> str | None:
    text = _clean_optional_string(value)
    if not text:
        return None
    return text[:128]


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _coerce_optional_confidence(value: Any) -> tuple[float | None, str | None]:
    if value is None or str(value).strip() == "":
        return None, None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None, "extraction_confidence_invalid"
    if parsed < 0.0 or parsed > 1.0:
        return None, "extraction_confidence_invalid"
    return parsed, None


def normalize_source_coverage(value: Any) -> tuple[str, str | None]:
    if value is None or str(value).strip() == "":
        return "unknown", None
    value = getattr(value, "value", value)
    normalized = str(value).strip().lower().replace("-", "_")
    if normalized not in VALID_SOURCE_COVERAGE:
        return "unknown", "source_coverage_invalid"
    return normalized, None


def merge_summary_quality_metadata(
    summary: SummaryJSON,
    *,
    validation_warnings: list[Any] | None = None,
    repair_attempted: bool | None = None,
    repair_succeeded: bool | None = None,
    structured_output_mode: Any = None,
    model_used: Any = None,
    source_coverage: Any = None,
    extraction_quality: Any = None,
    extraction_confidence: Any = None,
    prompt_injection_suspected: bool | None = None,
) -> SummaryJSON:
    """Merge safe quality metadata into a summary payload in-place."""
    raw_quality = summary.get(SUMMARY_QUALITY_KEY)
    warnings: list[str] = []
    if isinstance(raw_quality, dict):
        quality: dict[str, Any] = dict(raw_quality)
        for warning in quality.get("validation_warnings") or []:
            cleaned = _clean_warning(warning)
            if cleaned:
                warnings.append(cleaned)
    else:
        quality = {}
        if raw_quality is not None:
            warnings.append("summary_quality_invalid")

    for warning in validation_warnings or []:
        cleaned = _clean_warning(warning)
        if cleaned:
            warnings.append(cleaned)

    if repair_attempted is not None:
        quality["repair_attempted"] = bool(repair_attempted) or _coerce_bool(
            quality.get("repair_attempted")
        )
    else:
        quality["repair_attempted"] = _coerce_bool(quality.get("repair_attempted"))

    if repair_succeeded is not None:
        quality["repair_succeeded"] = bool(repair_succeeded) or _coerce_bool(
            quality.get("repair_succeeded")
        )
    else:
        quality["repair_succeeded"] = _coerce_bool(quality.get("repair_succeeded"))

    if structured_output_mode is not None:
        quality["structured_output_mode"] = _clean_optional_string(structured_output_mode)
    else:
        quality["structured_output_mode"] = _clean_optional_string(
            quality.get("structured_output_mode")
        )

    if model_used is not None:
        quality["model_used"] = _clean_optional_string(model_used)
    else:
        quality["model_used"] = _clean_optional_string(quality.get("model_used"))

    coverage_input = (
        source_coverage if source_coverage is not None else quality.get("source_coverage")
    )
    coverage, coverage_warning = normalize_source_coverage(coverage_input)
    quality["source_coverage"] = coverage
    if coverage_warning:
        warnings.append(coverage_warning)

    if extraction_quality is not None:
        quality["extraction_quality"] = _clean_optional_string(extraction_quality)
    else:
        quality["extraction_quality"] = _clean_optional_string(quality.get("extraction_quality"))

    confidence_input = (
        extraction_confidence
        if extraction_confidence is not None
        else quality.get("extraction_confidence")
    )
    confidence, confidence_warning = _coerce_optional_confidence(confidence_input)
    quality["extraction_confidence"] = confidence
    if confidence_warning:
        warnings.append(confidence_warning)

    if prompt_injection_suspected is not None:
        quality["prompt_injection_suspected"] = bool(prompt_injection_suspected)
    else:
        quality["prompt_injection_suspected"] = _coerce_bool(
            quality.get("prompt_injection_suspected")
        )

    seen: set[str] = set()
    deduped_warnings: list[str] = []
    for warning in warnings:
        if warning not in seen:
            seen.add(warning)
            deduped_warnings.append(warning)
    quality["validation_warnings"] = deduped_warnings
    summary[SUMMARY_QUALITY_KEY] = quality
    return summary


def sync_prompt_injection_quality(summary: SummaryJSON) -> SummaryJSON:
    quality = summary.get("quality")
    suspected = (
        bool(quality.get("prompt_injection_suspected")) if isinstance(quality, dict) else False
    )
    return merge_summary_quality_metadata(summary, prompt_injection_suspected=suspected)


def infer_source_coverage(
    *,
    content_text: str | None = None,
    content_source: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Infer extraction coverage from source metadata without overclaiming."""
    metadata = metadata if isinstance(metadata, dict) else {}
    explicit = metadata.get("source_coverage")
    if explicit:
        return normalize_source_coverage(explicit)[0]

    source = str(content_source or metadata.get("content_source") or "").strip().lower()
    text = (content_text or "")[:4000].lower()
    if "abstract_only" in source or "[pdf unavailable:" in text:
        return "abstract_only"
    if metadata.get("transcript_missing") or "transcript_missing" in source:
        return "transcript_missing"
    if "transcript unavailable" in text or "no transcript" in text:
        return "transcript_missing"
    if "partial" in source or metadata.get("partial") is True:
        return "partial"
    if metadata.get("truncated") is True or metadata.get("was_truncated") is True:
        return "partial"
    if source in _FULL_SOURCE_MARKERS:
        return "full"
    return "unknown"
