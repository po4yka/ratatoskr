"""Shared search scoring, filtering, and presentation helpers."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.application.services.topic_search_utils import ensure_mapping
from app.core.logging_utils import get_logger
from app.core.time_utils import isotime  # noqa: F401 - re-export; canonical def lives in app.core

logger = get_logger(__name__)

_HASHTAG_RE = re.compile(r"#([\w-]{1,50})", re.UNICODE)
_ENTITY_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b")


@dataclass(frozen=True)
class SearchFilters:
    """Normalized filter bundle shared by search endpoints."""

    language: str | None = None
    tags: list[str] | None = None
    domains: list[str] | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_read: bool | None = None
    is_favorited: bool | None = None


def query_tokens(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[\w-]{2,}", text, re.UNICODE)}


def lexical_overlap(query: str, text: str) -> float:
    query_terms = query_tokens(query)
    if not query_terms:
        return 0.0
    body_terms = query_tokens(text)
    if not body_terms:
        return 0.0
    return len(query_terms.intersection(body_terms)) / len(query_terms)


def extract_query_tags(query: str) -> list[str]:
    tags = [f"#{match.lower()}" for match in _HASHTAG_RE.findall(query)]
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        if tag in seen:
            continue
        seen.add(tag)
        result.append(tag)
    return result


def infer_intent(query: str) -> str:
    lowered = query.strip().lower()
    if lowered.startswith(("similar to ", "like ")):
        return "similarity"
    if lowered.endswith("?") or lowered.startswith(
        ("why ", "how ", "what ", "which ", "who ", "when ")
    ):
        return "question"
    if _HASHTAG_RE.search(lowered):
        return "topic"
    if _ENTITY_RE.search(query):
        return "entity"
    return "keyword"


def resolve_mode(requested_mode: str, intent: str) -> str:
    if requested_mode != "auto":
        return requested_mode
    if intent in {"question", "similarity", "entity", "topic"}:
        return "hybrid"
    return "keyword"


def freshness_score(created_at: Any) -> float:
    if not created_at:
        return 0.0
    if isinstance(created_at, str):
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    else:
        created = created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    age_days = max(0.0, (now - created).total_seconds() / 86400.0)
    return max(0.0, min(1.0, math.exp(-age_days / 45.0)))


def popularity_score(summary: dict[str, Any], payload: dict[str, Any]) -> float:
    favorited = 1.0 if summary.get("is_favorited") else 0.0
    key_stats = payload.get("key_stats") or []
    answered = payload.get("answered_questions") or []
    richness = min(1.0, (len(key_stats) + len(answered)) / 12.0)
    return max(0.0, min(1.0, 0.7 * favorited + 0.3 * richness))


def score_result(
    *,
    mode: str,
    fts_score: float,
    semantic_score: float,
    freshness: float,
    popularity: float,
    lexical: float,
) -> float:
    if mode == "keyword":
        return (0.62 * fts_score) + (0.2 * freshness) + (0.1 * popularity) + (0.08 * lexical)
    if mode == "semantic":
        return (0.65 * semantic_score) + (0.18 * freshness) + (0.09 * popularity) + (0.08 * lexical)
    return (
        (0.35 * fts_score)
        + (0.43 * semantic_score)
        + (0.12 * freshness)
        + (0.06 * popularity)
        + (0.04 * lexical)
    )


def build_match_explanation(
    *,
    mode: str,
    fts_score: float,
    semantic_score: float,
    freshness: float,
    popularity: float,
) -> tuple[list[str], str]:
    signals: list[str] = []
    if fts_score > 0.25:
        signals.append("keyword_match")
    if semantic_score > 0.35:
        signals.append("semantic_match")
    if freshness > 0.55:
        signals.append("recent")
    if popularity > 0.4:
        signals.append("popular")

    if not signals:
        signals.append("broad_match")

    reason = ", ".join(signals)
    return signals, f"Ranked by {mode} scoring using {reason}."


def passes_filters(
    *,
    request: dict[str, Any],
    summary: dict[str, Any],
    payload: dict[str, Any],
    filters: SearchFilters,
) -> bool:
    if filters.language and (summary.get("lang") or "").lower() != filters.language.lower():
        return False
    if filters.is_read is not None and bool(summary.get("is_read")) != bool(filters.is_read):
        return False
    if filters.is_favorited is not None and bool(summary.get("is_favorited")) != bool(
        filters.is_favorited
    ):
        return False

    metadata = ensure_mapping(payload.get("metadata"))
    domain = str(metadata.get("domain") or "").lower()
    if filters.domains:
        normalized_domains = {str(item).lower() for item in filters.domains if str(item).strip()}
        if normalized_domains and domain not in normalized_domains:
            return False

    topic_tags = payload.get("topic_tags") or []
    tag_set = {str(item).lower() for item in topic_tags if str(item).strip()}
    if filters.tags:
        required = []
        for raw in filters.tags:
            tag = str(raw).strip().lower()
            required.append(tag if tag.startswith("#") else f"#{tag}")
        if not set(required).intersection(tag_set):
            return False

    created_at = request.get("created_at")
    if isinstance(created_at, str):
        try:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            created_dt = None
    else:
        created_dt = created_at

    if created_dt and filters.start_date:
        try:
            start_dt = datetime.fromisoformat(filters.start_date)
            if created_dt.date() < start_dt.date():
                return False
        except ValueError:
            logger.debug(
                "search_filter_invalid_start_date",
                extra={"start_date": filters.start_date},
            )
    if created_dt and filters.end_date:
        try:
            end_dt = datetime.fromisoformat(filters.end_date)
            if created_dt.date() > end_dt.date():
                return False
        except ValueError:
            logger.debug(
                "search_filter_invalid_end_date",
                extra={"end_date": filters.end_date},
            )

    return True


def build_facets(results: list[dict[str, Any]]) -> dict[str, Any]:
    domains: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    tags: Counter[str] = Counter()
    read_states: Counter[str] = Counter()

    for item in results:
        domain = item.get("domain")
        if domain:
            domains[str(domain).lower()] += 1
        lang = item.get("lang")
        if lang:
            languages[str(lang).lower()] += 1
        for tag in item.get("topic_tags", []) or []:
            if tag:
                tags[str(tag).lower()] += 1
        read_states["read" if item.get("is_read") else "unread"] += 1

    def top(counter: Counter[str], limit: int = 20) -> list[dict[str, Any]]:
        return [{"value": key, "count": count} for key, count in counter.most_common(limit)]

    return {
        "domains": top(domains),
        "languages": top(languages),
        "tags": top(tags),
        "read_states": top(read_states, limit=4),
    }
