"""
Summary management endpoints.

Provides CRUD operations for summaries.
"""

from datetime import datetime
from hashlib import sha256
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel as _BulkBaseModel, Field

from app.adapters.email.service import EmailDeliveryService
from app.api.aggregation_provenance import build_source_bundle
from app.api.dependencies.database import get_summary_read_model_use_case
from app.api.dependencies.search_resources import get_vector_search_service
from app.api.exceptions import FeatureDisabledError, ResourceNotFoundError, ValidationError
from app.api.models.digest import SendEmailRequest
from app.api.models.requests import (
    SaveReadingPositionRequest,
    SubmitFeedbackRequest,
    UpdateSummaryRequest,
)
from app.api.models.responses import (
    AggregationSourceBundle,
    BulkSummaryUpdateResponse,
    BulkSummaryUpdateSuccessResponse,
    DeleteSummaryResponse,
    DeleteSummarySuccessResponse,
    FeedbackResponse,
    FeedbackSuccessResponse,
    PaginationInfo,
    SaveReadingPositionResponse,
    SaveReadingPositionSuccessResponse,
    SummaryCompact,
    SummaryContent,
    SummaryContentData,
    SummaryContentSuccessResponse,
    SummaryDetail,
    SummaryDetailProcessing,
    SummaryDetailQuality,
    SummaryDetailRequest,
    SummaryDetailSource,
    SummaryDetailSuccessResponse,
    SummaryDetailSummary,
    SummaryListResponse,
    SummaryListStats,
    SummaryListSuccessResponse,
    RelatedRead,
    SummaryRecommendationsResponse,
    SummaryRecommendationsSuccessResponse,
    SummaryRelatedReadsResponse,
    SummaryRelatedReadsSuccessResponse,
    ToggleFavoriteResponse,
    ToggleFavoriteSuccessResponse,
    UpdateSummaryResponse,
    UpdateSummarySuccessResponse,
    success_response,
)
from app.api.routers.auth import get_current_user
from app.api.search_helpers import isotime
from app.application.dto.vector_search import VectorSearchHitDTO
from app.application.services.related_reads_service import RelatedReadsService
from app.application.services.topic_search_utils import ensure_mapping
from app.application.use_cases.summary_read_model import SummaryReadModelUseCase
from app.config import load_config
from app.core.html_utils import clean_markdown_article_text, html_to_text
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC

logger = get_logger(__name__)
router = APIRouter()
BULK_SUMMARY_MAX_IDS = 500

# Internal schema stores "med"; API contract exposes "medium".
_HR_NORMALIZE: dict[str, str] = {"med": "medium"}
_SAFE_SOURCE_COVERAGE = {"full", "partial", "abstract_only", "transcript_missing", "unknown"}


class _RelatedReadsVectorAdapter:
    def __init__(
        self,
        vector_search: Any,
        *,
        user_id: int,
        user_scope: str | None,
        max_results: int,
    ) -> None:
        self._vector_search = vector_search
        self._user_id = user_id
        self._user_scope = user_scope
        self._max_results = max_results

    async def search(
        self,
        query: str,
        *,
        correlation_id: str | None = None,
    ) -> list[VectorSearchHitDTO]:
        result = await self._vector_search.search(
            query,
            user_scope=self._user_scope,
            user_id=self._user_id,
            limit=self._max_results,
            correlation_id=correlation_id,
        )
        return [
            VectorSearchHitDTO(
                request_id=item.request_id,
                summary_id=item.summary_id,
                similarity_score=item.similarity_score,
                url=item.url,
                title=item.title,
                snippet=item.snippet,
                source=item.source,
                published_at=item.published_at,
            )
            for item in result.results
        ]


def _normalize_hallucination_risk(raw: str) -> Literal["low", "medium", "high", "unknown"]:
    """Map internal short-form values to the API contract enum."""
    result = _HR_NORMALIZE.get(raw, raw)
    if result not in {"low", "medium", "high", "unknown"}:
        return "unknown"
    return cast("Literal['low', 'medium', 'high', 'unknown']", result)


def _safe_summary_quality(raw: Any) -> SummaryDetailQuality:
    quality = ensure_mapping(raw)
    warnings = [
        str(warning)[:128]
        for warning in quality.get("validation_warnings", []) or []
        if str(warning).strip()
    ]
    source_coverage = str(quality.get("source_coverage") or "unknown").strip().lower()
    if source_coverage not in _SAFE_SOURCE_COVERAGE:
        source_coverage = "unknown"
    extraction_confidence = quality.get("extraction_confidence")
    if extraction_confidence is not None:
        try:
            extraction_confidence = float(extraction_confidence)
        except (TypeError, ValueError):
            extraction_confidence = None
    return SummaryDetailQuality(
        validation_warnings=warnings,
        repair_attempted=bool(quality.get("repair_attempted")),
        repair_succeeded=bool(quality.get("repair_succeeded")),
        structured_output_mode=quality.get("structured_output_mode"),
        model_used=quality.get("model_used"),
        source_coverage=cast(
            "Literal['full', 'partial', 'abstract_only', 'transcript_missing', 'unknown']",
            source_coverage,
        ),
        extraction_quality=quality.get("extraction_quality"),
        extraction_confidence=extraction_confidence,
        prompt_injection_suspected=bool(quality.get("prompt_injection_suspected")),
    )


def _safe_compact_summary_quality(json_payload: dict[str, Any]) -> SummaryDetailQuality:
    quality = {
        **ensure_mapping(json_payload.get("quality")),
        **ensure_mapping(json_payload.get("summary_quality")),
    }
    return _safe_summary_quality(quality)


def _format_summary_email_content(
    json_payload: dict[str, Any], request_data: dict[str, Any]
) -> str:
    parts: list[str] = []
    metadata = ensure_mapping(json_payload.get("metadata"))
    title = metadata.get("title") or request_data.get("input_url")
    if title:
        parts.append(f"# {title}")
    tldr = json_payload.get("tldr")
    if tldr:
        parts.append(str(tldr))
    summary_250 = json_payload.get("summary_250")
    if summary_250:
        parts.append(str(summary_250))
    key_ideas = json_payload.get("key_ideas")
    if isinstance(key_ideas, list) and key_ideas:
        parts.append("Key ideas:\n" + "\n".join(f"- {item}" for item in key_ideas[:10]))
    source_url = request_data.get("input_url") or request_data.get("normalized_url")
    if source_url:
        parts.append(f"Read original: {source_url}")
    return "\n\n".join(parts)


def _build_summary_source_bundle(raw: Any) -> AggregationSourceBundle | None:
    if raw is None:
        return None
    if isinstance(raw, AggregationSourceBundle):
        return raw
    bundle_data = ensure_mapping(raw)
    session_data = ensure_mapping(bundle_data.get("session"))
    items = bundle_data.get("items")
    if not isinstance(items, list):
        return None
    session_id = session_data.get("id")
    if session_id is None and items:
        session_id = ensure_mapping(items[0]).get("aggregation_session_id")
    if session_id is None:
        return None
    return build_source_bundle(
        session_id=int(session_id),
        correlation_id=session_data.get("correlation_id"),
        status=session_data.get("status"),
        persisted_items=[ensure_mapping(item) for item in items],
    )


def _get_summary_use_case() -> SummaryReadModelUseCase:
    """Build the summary read-model use case for API handlers."""
    return get_summary_read_model_use_case()


async def _get_related_reads_service(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    vector_search: Any = Depends(get_vector_search_service),
) -> RelatedReadsService:
    """Build a request-scoped related-reads service for API handlers."""
    from app.di.api import resolve_api_runtime

    runtime = resolve_api_runtime(request)
    cfg = runtime.cfg
    if not cfg.runtime.related_reads_enabled:
        raise FeatureDisabledError("related-reads")

    max_results = 10
    return RelatedReadsService(
        _RelatedReadsVectorAdapter(
            vector_search,
            user_id=user["user_id"],
            user_scope=cfg.vector_store.user_scope,
            max_results=max_results,
        ),
        min_similarity=cfg.runtime.related_reads_min_similarity,
        max_results=max_results,
    )


def _extract_request_fields(
    summary_dict: dict[str, Any],
) -> tuple[int | None, str, str]:
    """Extract (request_id, input_url, normalized_url) from a joined summary dict.

    The repository may return ``request`` as a nested dict (normal case) or as
    a bare integer ID (legacy/partial join).  Both shapes are handled here so
    callers don't duplicate the guard.
    """
    request_data = summary_dict.get("request") or {}
    if isinstance(request_data, int):
        return request_data, "", ""
    return (
        request_data.get("id", summary_dict.get("request_id")),
        request_data.get("input_url", ""),
        request_data.get("normalized_url", ""),
    )


def _build_summary_compact(summary_dict: dict[str, Any]) -> SummaryCompact:
    """Build a SummaryCompact response model from a joined summary dict."""
    request_id, input_url, normalized_url = _extract_request_fields(summary_dict)
    json_payload = ensure_mapping(summary_dict.get("json_payload"))
    metadata = ensure_mapping(json_payload.get("metadata"))
    quality = _safe_compact_summary_quality(json_payload)
    return SummaryCompact(
        id=summary_dict.get("id"),
        request_id=request_id,
        title=metadata.get("title", "Untitled"),
        domain=metadata.get("domain", ""),
        url=input_url or normalized_url or "",
        tldr=json_payload.get("tldr", ""),
        summary_250=json_payload.get("summary_250", ""),
        reading_time_min=json_payload.get("estimated_reading_time_min", 0),
        topic_tags=json_payload.get("topic_tags", []),
        is_read=summary_dict.get("is_read", False),
        is_favorited=summary_dict.get("is_favorited", False),
        lang=summary_dict.get("lang") or "auto",
        created_at=isotime(summary_dict.get("created_at")),
        confidence=json_payload.get("confidence", 0.0),
        hallucination_risk=_normalize_hallucination_risk(
            json_payload.get("hallucination_risk", "unknown")
        ),
        image_url=metadata.get("image") or metadata.get("og:image") or metadata.get("ogImage"),
        source_coverage=quality.source_coverage,
        repair_attempted=quality.repair_attempted,
        repair_succeeded=quality.repair_succeeded,
        prompt_injection_suspected=quality.prompt_injection_suspected,
        validation_warning_count=len(quality.validation_warnings),
    )


def _resolve_content(
    crawl_result: dict[str, Any],
    request_data: dict[str, Any],
    output_format: str,
) -> tuple[str, str, str]:
    """Resolve article content for the requested output format.

    Returns (content_value, content_mime, resolved_format).
    Raises ResourceNotFoundError (ValueError) if no content is available.
    """
    if crawl_result.get("content_markdown"):
        content_source: str = crawl_result["content_markdown"]
        source_format = "markdown"
        content_type = "text/markdown"
    elif crawl_result.get("content_html"):
        content_source = crawl_result["content_html"]
        source_format = "html"
        content_type = "text/html"
    elif request_data.get("content_text"):
        content_source = request_data["content_text"]
        source_format = "text"
        content_type = "text/plain"
    else:
        raise ValueError("no_content")

    resolved_format = output_format or "markdown"
    content_value = content_source
    content_mime = content_type

    if resolved_format == "text":
        if source_format == "markdown":
            content_value = clean_markdown_article_text(content_source)
        elif source_format == "html":
            content_value = html_to_text(content_source)
        content_mime = "text/plain"
    elif source_format == "markdown":
        content_value = content_source
        content_mime = "text/markdown"
    elif source_format == "html":
        content_value = html_to_text(content_source)
        content_mime = "text/plain"
        resolved_format = "text"
    else:
        content_value = content_source
        content_mime = "text/plain"
        resolved_format = "text"

    return content_value, content_mime, resolved_format


@router.get("", response_model=SummaryListSuccessResponse)
async def get_summaries(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    is_read: bool | None = Query(None),
    is_favorited: bool | None = Query(None),
    lang: str | None = Query(None, pattern="^(en|ru|auto)$"),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    sort: str = Query("created_at_desc", pattern="^(created_at_desc|created_at_asc)$"),
    search: str | None = Query(
        None,
        max_length=200,
        description="Case-insensitive substring match on the article title.",
    ),
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """
    Get paginated list of summaries.

    Query Parameters:
    - limit: Items per page (1-100, default 20)
    - offset: Pagination offset (default 0)
    - is_read: Filter by read status (optional)
    - lang: Filter by language (en/ru/auto)
    - start_date: Filter by creation date (ISO 8601)
    - end_date: Filter by creation date (ISO 8601)
    - sort: Sort order (created_at_desc/created_at_asc)
    - search: Case-insensitive substring match on the article title
    """

    # Use service layer for business logic
    summaries, total, unread_count = await use_case.get_user_summaries(
        user_id=user["user_id"],
        limit=limit,
        offset=offset,
        is_read=is_read,
        is_favorited=is_favorited,
        lang=lang,
        start_date=start_date,
        end_date=end_date,
        sort=sort,
        search=search,
    )

    # Build response from dictionary data
    summary_list = [_build_summary_compact(s) for s in summaries]

    pagination = PaginationInfo(
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )

    return success_response(
        SummaryListResponse(
            summaries=summary_list,
            pagination=pagination,
            stats=SummaryListStats(total_summaries=total, unread_count=unread_count),
        ),
        pagination=pagination,
    )


@router.get("/by-url", response_model=SummaryDetailSuccessResponse)
async def get_summary_by_url(
    url: str = Query(..., description="Original URL of the article"),
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """Get a single summary (article) by its original URL."""
    summary_id = await use_case.get_summary_id_by_url_for_user(user_id=user["user_id"], url=url)
    if not summary_id:
        raise ResourceNotFoundError("Article", url)

    return await get_summary(summary_id=summary_id, user=user, use_case=use_case)


@router.get("/recommendations", response_model=SummaryRecommendationsSuccessResponse)
async def get_recommendations(
    limit: int = Query(10, ge=1, le=50),
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """Get personalized summary recommendations based on reading history."""
    user_id = user["user_id"]

    # Get recently-read summaries to determine interest tags
    read_summaries, _, _ = await use_case.get_user_summaries(
        user_id=user_id,
        limit=10,
        offset=0,
        is_read=True,
        sort="created_at_desc",
    )

    interest_tags: set[str] = set()
    for s in read_summaries:
        payload = ensure_mapping(s.get("json_payload"))
        for tag in payload.get("topic_tags", []):
            if isinstance(tag, str):
                interest_tags.add(tag.lower())

    # Get unread summaries to recommend from
    unread_summaries, _, _ = await use_case.get_user_summaries(
        user_id=user_id,
        limit=100,
        offset=0,
        is_read=False,
        sort="created_at_desc",
    )

    # Score by tag overlap
    def _score(s: dict[str, Any]) -> int:
        payload = ensure_mapping(s.get("json_payload"))
        tags = {t.lower() for t in payload.get("topic_tags", []) if isinstance(t, str)}
        return len(tags & interest_tags)

    scored = sorted(unread_summaries, key=_score, reverse=True)
    top = scored[:limit]

    summary_list = [_build_summary_compact(s) for s in top]

    return success_response(
        SummaryRecommendationsResponse(
            recommendations=summary_list,
            reason="based_on_reading_history" if interest_tags else "most_recent_unread",
            count=len(summary_list),
        )
    )


@router.get("/{summary_id}/related", response_model=SummaryRelatedReadsSuccessResponse)
async def get_related_reads(
    summary_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
    related_reads_service: RelatedReadsService = Depends(_get_related_reads_service),
) -> Any:
    """Get vector-similar summaries related to a user's summary."""
    context = await use_case.get_summary_context_for_user(
        user_id=user["user_id"],
        summary_id=summary_id,
    )
    if not context:
        raise ResourceNotFoundError("Summary", summary_id)

    summary = ensure_mapping(context.get("summary"))
    summary_payload = ensure_mapping(summary.get("json_payload"))
    request_id = context.get("request_id")
    related = await related_reads_service.find_related(
        summary_payload,
        exclude_request_id=int(request_id) if request_id is not None else None,
        language=summary.get("lang"),
    )
    return success_response(
        SummaryRelatedReadsResponse(
            summary_id=summary_id,
            related=[
                RelatedRead(
                    summary_id=item.summary_id,
                    request_id=item.request_id,
                    title=item.title,
                    age_label=item.age_label,
                    similarity_score=item.similarity_score,
                )
                for item in related
            ],
            count=len(related),
        )
    )


@router.get("/{summary_id}", response_model=SummaryDetailSuccessResponse)
async def get_summary(
    summary_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """Get a single summary with full details."""
    context = await use_case.get_summary_context_for_user(
        user_id=user["user_id"],
        summary_id=summary_id,
    )
    if not context:
        raise ResourceNotFoundError("Summary", summary_id)

    summary = context["summary"]
    request_data = context["request"]
    crawl_result = context["crawl_result"]
    transcription_artifact = context.get("transcription_artifact")
    llm_calls = context["llm_calls"]

    # Build source metadata
    source = {}
    if crawl_result:
        metadata = crawl_result.get("metadata_json") or {}
        source = {
            "url": crawl_result.get("source_url"),
            "title": metadata.get("title"),
            "domain": metadata.get("domain"),
            "author": metadata.get("author"),
            "published_at": metadata.get("published_at"),
            "word_count": summary.get("word_count"),
            "content_type": metadata.get("content_type")
            or metadata.get("og:type")
            or metadata.get("type"),
        }

    # Build processing info
    processing = {}
    if llm_calls:
        latest_call = llm_calls[-1]
        processing = {
            "model": latest_call.get("model"),
            "tokens_used": (latest_call.get("tokens_prompt") or 0)
            + (latest_call.get("tokens_completion") or 0),
            "latency_ms": sum(call.get("latency_ms") or 0 for call in llm_calls),
            "crawl_latency_ms": crawl_result.get("latency_ms") if crawl_result else None,
        }

    # Build SummaryDetailSummary from json_payload
    json_payload = ensure_mapping(summary.get("json_payload"))
    entities_raw = ensure_mapping(json_payload.get("entities"))
    readability_raw = ensure_mapping(json_payload.get("readability"))

    summary_detail = {
        "summary_250": json_payload.get("summary_250", ""),
        "summary_1000": json_payload.get("summary_1000", ""),
        "tldr": json_payload.get("tldr", ""),
        "key_ideas": json_payload.get("key_ideas", []),
        "topic_tags": json_payload.get("topic_tags", []),
        "entities": {
            "people": entities_raw.get("people", []),
            "organizations": entities_raw.get("organizations", []),
            "locations": entities_raw.get("locations", []),
        },
        "estimated_reading_time_min": json_payload.get("estimated_reading_time_min", 0),
        "key_stats": json_payload.get("key_stats", []),
        "answered_questions": json_payload.get("answered_questions", []),
        "readability": (
            {
                "method": readability_raw.get("method", ""),
                "score": readability_raw.get("score", 0.0),
                "level": readability_raw.get("level", ""),
            }
            if readability_raw
            else None
        ),
        "seo_keywords": json_payload.get("seo_keywords", []),
    }

    request_detail = {
        "id": str(request_data.get("id", "")),
        "type": request_data.get("type", ""),
        "url": request_data.get("input_url"),
        "normalized_url": request_data.get("normalized_url"),
        "dedupe_hash": request_data.get("dedupe_hash"),
        "status": request_data.get("status", ""),
        "lang_detected": request_data.get("lang_detected"),
        "created_at": isotime(request_data.get("created_at")),
        "updated_at": isotime(request_data.get("updated_at") or request_data.get("created_at")),
    }

    source_detail = {
        "url": source.get("url"),
        "title": source.get("title"),
        "domain": source.get("domain"),
        "author": source.get("author"),
        "published_at": source.get("published_at"),
        "word_count": source.get("word_count"),
        "content_type": source.get("content_type"),
        "transcript": (
            transcription_artifact.get("plain_text")
            if isinstance(transcription_artifact, dict)
            else None
        ),
    }

    processing_detail = {
        "model_used": processing.get("model"),
        "tokens_used": processing.get("tokens_used"),
        "processing_time_ms": processing.get("latency_ms"),
        "crawl_time_ms": processing.get("crawl_latency_ms"),
        "confidence": json_payload.get("confidence"),
        "hallucination_risk": _normalize_hallucination_risk(
            json_payload.get("hallucination_risk") or "unknown"
        ),
        "quality": _safe_summary_quality(json_payload.get("summary_quality")),
    }

    return success_response(
        SummaryDetail(
            summary=SummaryDetailSummary(**summary_detail),
            request=SummaryDetailRequest(**request_detail),
            source=SummaryDetailSource(**source_detail),
            processing=SummaryDetailProcessing(**processing_detail),
            source_bundle=_build_summary_source_bundle(context.get("aggregation_source_bundle")),
            reading_progress=summary.get("reading_progress"),
            last_read_offset=summary.get("last_read_offset"),
        )
    )


@router.post("/{summary_id}/email")
async def email_summary(
    summary_id: int,
    body: SendEmailRequest,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """Send an individual summary to a verified email address."""
    context = await use_case.get_summary_context_for_user(
        user_id=user["user_id"],
        summary_id=summary_id,
    )
    if not context:
        raise ResourceNotFoundError("Summary", summary_id)

    summary = context["summary"]
    request_data = context["request"]
    json_payload = ensure_mapping(summary.get("json_payload"))
    metadata = ensure_mapping(json_payload.get("metadata"))
    title = metadata.get("title") or request_data.get("input_url") or f"Summary {summary_id}"
    content = _format_summary_email_content(json_payload, request_data)
    payload = await EmailDeliveryService(load_config().email).send_custom_content(
        user_id=user["user_id"],
        address_id=body.email_address_id,
        subject=f"Ratatoskr summary: {title}",
        content=content,
        purpose="summary",
        metadata={"summary_id": summary_id},
    )
    return success_response(payload)


@router.get("/{summary_id}/content", response_model=SummaryContentSuccessResponse)
async def get_summary_content(
    summary_id: int,
    format: str = Query("markdown", pattern="^(markdown|text)$"),
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """Get full article content for offline reading."""
    context = await use_case.get_summary_context_for_user(
        user_id=user["user_id"],
        summary_id=summary_id,
    )
    if not context:
        raise ResourceNotFoundError("Summary", summary_id)

    summary = context["summary"]
    request_data = context["request"]
    request_id = context["request_id"]
    crawl_result = context["crawl_result"]

    if not crawl_result:
        raise ResourceNotFoundError("Content", summary_id)

    metadata = ensure_mapping(crawl_result.get("metadata_json"))
    summary_metadata = ensure_mapping(ensure_mapping(summary.get("json_payload")).get("metadata"))
    source_url = (
        crawl_result.get("source_url")
        or request_data.get("input_url")
        or request_data.get("normalized_url")
    )
    title = metadata.get("title") or summary_metadata.get("title")
    domain = metadata.get("domain") or summary_metadata.get("domain")

    try:
        content_value, content_mime, output_format = _resolve_content(
            crawl_result, request_data, format
        )
    except ValueError as exc:
        raise ResourceNotFoundError("Content", summary_id) from exc

    checksum = sha256(content_value.encode("utf-8")).hexdigest() if content_value else None
    size_bytes = len(content_value.encode("utf-8")) if content_value else None
    retrieved_dt = (
        crawl_result.get("updated_at") or crawl_result.get("created_at") or datetime.now(UTC)
    )

    return success_response(
        SummaryContentData(
            content=SummaryContent(
                summary_id=summary.get("id"),
                request_id=request_id,
                format=cast('Literal["markdown", "text", "html"]', output_format),
                content=content_value,
                content_type=cast(
                    'Literal["text/markdown", "text/plain", "text/html"]', content_mime
                ),
                lang=summary.get("lang"),
                source_url=source_url,
                title=title,
                domain=domain,
                retrieved_at=isotime(retrieved_dt),
                size_bytes=size_bytes,
                checksum_sha256=checksum,
            )
        )
    )


@router.get("/{summary_id}/export")
async def export_summary(
    summary_id: int,
    format: str = Query("pdf", pattern="^(pdf|md|html)$"),
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """Export a summary as PDF, Markdown, or HTML."""
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    from app.adapters.external.formatting.export_formatter import ExportFormatter
    from app.adapters.external.formatting.export_temp_files import cleanup_export_file
    from app.api.dependencies.database import get_session_manager

    summary = await use_case.get_summary_by_id_for_user(
        user_id=user["user_id"],
        summary_id=summary_id,
    )
    if not summary:
        raise ResourceNotFoundError("Summary", summary_id)

    db = get_session_manager()
    formatter = ExportFormatter(db)
    file_path, filename = await formatter.export_summary(
        summary_id=str(summary_id),
        export_format=format,
    )
    if not file_path or not filename:
        raise ResourceNotFoundError("Export", summary_id)

    media_type_map = {
        "pdf": "application/pdf",
        "md": "text/markdown",
        "html": "text/html",
    }
    media_type = media_type_map.get(format, "application/octet-stream")

    return FileResponse(
        path=file_path,
        filename=filename,
        media_type=media_type,
        background=BackgroundTask(cleanup_export_file, file_path),
    )


@router.patch("/{summary_id}", response_model=UpdateSummarySuccessResponse)
async def update_summary(
    summary_id: int,
    update: UpdateSummaryRequest,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """Update summary metadata (e.g., mark as read)."""
    updated_summary = await use_case.update_summary(
        user_id=user["user_id"],
        summary_id=summary_id,
        is_read=update.is_read,
    )
    if not updated_summary:
        raise ResourceNotFoundError("Summary", summary_id)

    resolved_is_read = bool(updated_summary.get("is_read"))

    return success_response(
        UpdateSummaryResponse(
            id=summary_id,
            is_read=resolved_is_read,
            updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
    )


@router.patch("/{summary_id}/reading-position", response_model=SaveReadingPositionSuccessResponse)
async def save_reading_position(
    summary_id: int,
    body: SaveReadingPositionRequest,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """Save the reading position (scroll progress) for a summary."""
    updated = await use_case.update_reading_progress(
        user_id=user["user_id"],
        summary_id=summary_id,
        progress=body.progress,
        last_read_offset=body.last_read_offset,
    )
    if not updated:
        raise ResourceNotFoundError("Summary", summary_id)

    return success_response(
        SaveReadingPositionResponse(
            id=summary_id,
            progress=body.progress,
            last_read_offset=body.last_read_offset,
        )
    )


@router.delete("/{summary_id}", response_model=DeleteSummarySuccessResponse)
async def delete_summary(
    summary_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """Delete a summary (soft delete)."""
    deleted = await use_case.soft_delete_summary(user_id=user["user_id"], summary_id=summary_id)
    if not deleted:
        raise ResourceNotFoundError("Summary", summary_id)

    return success_response(
        DeleteSummaryResponse(
            id=summary_id,
            deleted_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
    )


class _BulkMarkReadRequest(_BulkBaseModel):
    summary_ids: list[int] = Field(default_factory=list, max_length=BULK_SUMMARY_MAX_IDS)


class _BulkFavoriteRequest(_BulkBaseModel):
    summary_ids: list[int] = Field(default_factory=list, max_length=BULK_SUMMARY_MAX_IDS)
    value: bool = True


class _BulkDeleteRequest(_BulkBaseModel):
    summary_ids: list[int] = Field(default_factory=list, max_length=BULK_SUMMARY_MAX_IDS)


@router.post("/bulk/mark-read", response_model=BulkSummaryUpdateSuccessResponse)
async def bulk_mark_read(
    body: _BulkMarkReadRequest,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """Mark multiple summaries as read in one round-trip.

    Accepts up to 500 summary IDs. Cross-user IDs are silently skipped
    (never raises) so a single malformed batch cannot enumerate
    another user's summary IDs.
    """
    try:
        updated = await use_case.bulk_mark_as_read(
            user_id=user["user_id"], summary_ids=body.summary_ids
        )
    except ValueError as exc:
        raise ValidationError(str(exc), details={"field": "summary_ids"}) from exc
    return success_response(BulkSummaryUpdateResponse(updated=updated))


@router.post("/bulk/favorite", response_model=BulkSummaryUpdateSuccessResponse)
async def bulk_favorite(
    body: _BulkFavoriteRequest,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """Set or clear the favorite flag on multiple summaries.

    Pass ``value=true`` to favorite, ``false`` to unfavorite. Same
    user-scoping and 500-id cap as ``/bulk/mark-read``.
    """
    try:
        updated = await use_case.bulk_set_favorite(
            user_id=user["user_id"],
            summary_ids=body.summary_ids,
            value=body.value,
        )
    except ValueError as exc:
        raise ValidationError(str(exc), details={"field": "summary_ids"}) from exc
    return success_response(BulkSummaryUpdateResponse(updated=updated))


@router.post("/bulk/delete", response_model=BulkSummaryUpdateSuccessResponse)
async def bulk_delete(
    body: _BulkDeleteRequest,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """Soft-delete multiple summaries in one round-trip."""
    try:
        deleted = await use_case.bulk_delete(user_id=user["user_id"], summary_ids=body.summary_ids)
    except ValueError as exc:
        raise ValidationError(str(exc), details={"field": "summary_ids"}) from exc
    return success_response(BulkSummaryUpdateResponse(updated=deleted))


@router.post("/{summary_id}/favorite", response_model=ToggleFavoriteSuccessResponse)
async def toggle_favorite(
    summary_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """Toggle the favorite status of a summary."""
    is_favorited = await use_case.toggle_favorite(user_id=user["user_id"], summary_id=summary_id)
    if is_favorited is None:
        raise ResourceNotFoundError("Summary", summary_id)
    return success_response(ToggleFavoriteResponse(success=True, is_favorited=is_favorited))


@router.post("/{summary_id}/feedback", response_model=FeedbackSuccessResponse)
async def submit_feedback(
    summary_id: int,
    body: SubmitFeedbackRequest,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: SummaryReadModelUseCase = Depends(_get_summary_use_case),
) -> Any:
    """Submit or update feedback for a summary."""
    feedback = await use_case.submit_feedback(
        user_id=user["user_id"],
        summary_id=summary_id,
        rating=body.rating,
        issues=body.issues,
        comment=body.comment,
    )
    if not feedback:
        raise ResourceNotFoundError("Summary", summary_id)

    return success_response(
        FeedbackResponse(
            id=str(feedback["id"]),
            rating=feedback["rating"],
            issues=feedback["issues"],
            comment=feedback["comment"],
            created_at=isotime(feedback["created_at"]),
        )
    )
