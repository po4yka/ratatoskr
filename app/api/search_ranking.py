"""Ranking helpers shared by API search endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.api.models.responses import SearchResult
from app.api.search_helpers import (
    SearchFilters,
    build_match_explanation,
    extract_query_tags,
    freshness_score,
    isotime,
    lexical_overlap,
    passes_filters,
    popularity_score,
    score_result,
)
from app.application.services.topic_search_utils import ensure_mapping
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)


def _normalize_search_title(*candidates: Any) -> str:
    """Return the first usable title without trusting persisted/provider types."""
    return next(
        (candidate for candidate in candidates if isinstance(candidate, str) and candidate),
        "Untitled",
    )


def build_fts_hits(fts_results: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    hits: dict[int, dict[str, Any]] = {}
    for idx, row in enumerate(fts_results):
        try:
            req_id = int(row["request_id"])
        except (KeyError, TypeError, ValueError):
            logger.debug("search_result_request_id_parse_failed", extra={"row_index": idx})
            continue
        if req_id in hits:
            continue
        hits[req_id] = {
            "position": idx,
            "score": max(0.0, 1.0 - (idx / max(len(fts_results), 1))),
            "row": row,
        }
    return hits


async def build_semantic_hits(
    *,
    q: str,
    resolved_mode: str,
    filters: SearchFilters,
    user_id: int,
    fetch_limit: int,
    min_similarity: float,
    get_vector_service: Callable[[], Awaitable[Any]],
) -> dict[int, dict[str, Any]]:
    hits: dict[int, dict[str, Any]] = {}
    if resolved_mode not in {"semantic", "hybrid"}:
        return hits

    try:
        vector_service = await get_vector_service()
    except Exception:
        vector_service = None
        logger.warning("search_vector_unavailable", exc_info=True)

    semantic_tags = filters.tags or extract_query_tags(q) or None
    if vector_service is None:
        return hits

    semantic_results = await vector_service.search(
        q,
        language=filters.language,
        tags=semantic_tags,
        user_id=user_id,
        limit=fetch_limit,
        offset=0,
    )
    for idx, result in enumerate(semantic_results.results):
        if result.similarity_score < min_similarity:
            continue
        if result.request_id in hits:
            continue
        hits[result.request_id] = {
            "position": idx,
            "score": float(result.similarity_score),
            "row": result,
        }
    return hits


def candidate_request_ids(
    resolved_mode: str,
    fts_by_request_id: dict[int, dict[str, Any]],
    semantic_by_request_id: dict[int, dict[str, Any]],
) -> list[int]:
    if resolved_mode == "keyword":
        return list(fts_by_request_id.keys())
    if resolved_mode == "semantic":
        return list(semantic_by_request_id.keys())
    return list(dict.fromkeys([*fts_by_request_id.keys(), *semantic_by_request_id.keys()]))


def build_ranked_search_rows(
    *,
    q: str,
    resolved_mode: str,
    candidate_request_ids: list[int],
    requests_map: dict[int, dict[str, Any]],
    summaries_map: dict[int, dict[str, Any]],
    fts_by_request_id: dict[int, dict[str, Any]],
    semantic_by_request_id: dict[int, dict[str, Any]],
    filters: SearchFilters,
) -> list[dict[str, Any]]:
    ranked_rows: list[dict[str, Any]] = []
    for req_id in candidate_request_ids:
        request = requests_map.get(req_id)
        summary = summaries_map.get(req_id)
        if not request or not summary:
            continue

        payload = ensure_mapping(summary.get("json_payload"))
        metadata = ensure_mapping(payload.get("metadata"))
        if not passes_filters(
            request=request,
            summary=summary,
            payload=payload,
            filters=filters,
        ):
            continue

        fts_hit = fts_by_request_id.get(req_id)
        semantic_hit = semantic_by_request_id.get(req_id)
        fts_score = float(fts_hit["score"]) if fts_hit else 0.0
        semantic_score = float(semantic_hit["score"]) if semantic_hit else 0.0
        freshness = freshness_score(request.get("created_at"))
        popularity = popularity_score(summary, payload)
        title = _normalize_search_title(
            semantic_hit["row"].title if semantic_hit else None,
            fts_hit["row"].get("title") if fts_hit else None,
            metadata.get("title"),
        )
        snippet = (
            (semantic_hit["row"].snippet if semantic_hit and semantic_hit["row"].snippet else None)
            or (fts_hit["row"].get("snippet") if fts_hit else None)
            or payload.get("summary_250")
            or payload.get("tldr", "")
        )
        lexical = lexical_overlap(q, f"{title} {snippet or ''}")
        score = score_result(
            mode=resolved_mode,
            fts_score=fts_score,
            semantic_score=semantic_score,
            freshness=freshness,
            popularity=popularity,
            lexical=lexical,
        )
        signals, reason = build_match_explanation(
            mode=resolved_mode,
            fts_score=fts_score,
            semantic_score=semantic_score,
            freshness=freshness,
            popularity=popularity,
        )
        ranked_rows.append(
            {
                "request_id": req_id,
                "summary_id": summary.get("id"),
                "url": request.get("input_url") or request.get("normalized_url"),
                "title": title,
                "domain": metadata.get("domain")
                or (fts_hit["row"].get("source") if fts_hit else ""),
                "snippet": snippet,
                "tldr": payload.get("tldr", ""),
                "published_at": metadata.get("published_at")
                or (fts_hit["row"].get("published_at") if fts_hit else None),
                "created_at": isotime(request.get("created_at")),
                "relevance_score": round(score, 4),
                "topic_tags": payload.get("topic_tags", []),
                "is_read": summary.get("is_read", False),
                "lang": summary.get("lang"),
                "match_signals": signals,
                "match_explanation": reason,
                "score_breakdown": {
                    "fts": round(fts_score, 4),
                    "semantic": round(semantic_score, 4),
                    "freshness": round(freshness, 4),
                    "popularity": round(popularity, 4),
                    "lexical": round(lexical, 4),
                },
            }
        )
    ranked_rows.sort(key=lambda item: float(item.get("relevance_score", 0.0)), reverse=True)
    return ranked_rows


def rows_to_search_results(rows: list[dict[str, Any]]) -> list[SearchResult]:
    return [
        SearchResult(
            request_id=item["request_id"],
            summary_id=item["summary_id"],
            url=item["url"],
            title=item["title"],
            domain=item["domain"],
            snippet=item["snippet"],
            tldr=item["tldr"],
            published_at=item["published_at"],
            created_at=item["created_at"],
            relevance_score=item["relevance_score"],
            topic_tags=item["topic_tags"],
            is_read=item["is_read"],
            match_signals=item["match_signals"],
            match_explanation=item["match_explanation"],
            score_breakdown=item["score_breakdown"],
        )
        for item in rows
    ]


def build_semantic_filtered_rows(
    *,
    q: str,
    min_similarity: float,
    filters: SearchFilters,
    search_results: Any,
    requests_map: dict[int, dict[str, Any]],
    summaries_map: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    filtered_rows: list[dict[str, Any]] = []
    for result in search_results.results:
        request = requests_map.get(result.request_id)
        if not request:
            continue
        summary = summaries_map.get(result.request_id)
        if not summary:
            continue

        json_payload = ensure_mapping(summary.get("json_payload"))
        metadata = ensure_mapping(json_payload.get("metadata"))
        if result.similarity_score < min_similarity:
            continue
        if not passes_filters(
            request=request,
            summary=summary,
            payload=json_payload,
            filters=filters,
        ):
            continue

        snippet = result.snippet or json_payload.get("summary_250") or json_payload.get("tldr")
        freshness = freshness_score(request.get("created_at"))
        popularity = popularity_score(summary, json_payload)
        title = _normalize_search_title(result.title, metadata.get("title"))
        lexical = lexical_overlap(q, f"{title} {snippet or ''}")
        relevance = score_result(
            mode="semantic",
            fts_score=0.0,
            semantic_score=float(result.similarity_score),
            freshness=freshness,
            popularity=popularity,
            lexical=lexical,
        )
        signals, reason = build_match_explanation(
            mode="semantic",
            fts_score=0.0,
            semantic_score=float(result.similarity_score),
            freshness=freshness,
            popularity=popularity,
        )
        filtered_rows.append(
            {
                "request_id": result.request_id,
                "summary_id": summary.get("id"),
                "url": result.url or request.get("input_url") or request.get("normalized_url"),
                "title": title,
                "domain": metadata.get("domain") or metadata.get("source", ""),
                "snippet": snippet,
                "tldr": json_payload.get("tldr", ""),
                "published_at": metadata.get("published_at") or metadata.get("published"),
                "created_at": isotime(request.get("created_at")),
                "relevance_score": round(relevance, 4),
                "topic_tags": json_payload.get("topic_tags") or result.tags,
                "is_read": summary.get("is_read", False),
                "match_signals": signals,
                "match_explanation": reason,
                "score_breakdown": {
                    "fts": 0.0,
                    "semantic": round(float(result.similarity_score), 4),
                    "freshness": round(freshness, 4),
                    "popularity": round(popularity, 4),
                    "lexical": round(lexical, 4),
                },
                "lang": summary.get("lang"),
            }
        )
    filtered_rows.sort(key=lambda item: float(item.get("relevance_score", 0.0)), reverse=True)
    return filtered_rows
