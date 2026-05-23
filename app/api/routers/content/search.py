"""Search and discovery endpoints."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, Query, Request

from app.api.dependencies.database import get_search_read_model_use_case, get_session_manager
from app.api.exceptions import ProcessingError
from app.api.models.responses import success_response
from app.api.routers.auth import get_current_user
from app.api.search_helpers import SearchFilters
from app.api.services.search_service import SearchService
from app.core.logging_utils import get_logger
from app.infrastructure.cache.trending_cache import get_trending_payload

if TYPE_CHECKING:
    from app.application.use_cases.search_read_model import SearchReadModelUseCase

logger = get_logger(__name__)
router = APIRouter()


def _get_search_service(request: Request) -> SearchService:
    """Resolve the search orchestration service from shared dependencies."""
    read_model: SearchReadModelUseCase = get_search_read_model_use_case(request=request)
    return SearchService(search_read_model=read_model)


def _search_filter_params(
    language: str | None = Query(None, min_length=2, max_length=10),
    tags: list[str] | None = Query(None),
    domains: list[str] | None = Query(None),
    start_date: str | None = Query(None, description="ISO date (YYYY-MM-DD)"),
    end_date: str | None = Query(None, description="ISO date (YYYY-MM-DD)"),
    is_read: bool | None = Query(None),
    is_favorited: bool | None = Query(None),
) -> SearchFilters:
    return SearchFilters(
        language=language,
        tags=tags,
        domains=domains,
        start_date=start_date,
        end_date=end_date,
        is_read=is_read,
        is_favorited=is_favorited,
    )


@router.get("/search")
async def search_summaries(
    q: str = Query(..., min_length=2, max_length=200),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    mode: str = Query("auto", pattern="^(auto|keyword|semantic|hybrid)$"),
    min_similarity: float = Query(0.2, ge=0.0, le=1.0),
    filters: SearchFilters = Depends(_search_filter_params),
    user: dict[str, Any] = Depends(get_current_user),
    search_service: SearchService = Depends(_get_search_service),
) -> Any:
    """
    Full-text search across all summaries using PostgreSQL text search.

    Search Syntax:
    - Wildcard: bitcoin*
    - Phrase: "artificial intelligence"
    - Boolean: blockchain AND crypto
    - Exclusion: crypto NOT bitcoin
    """
    try:
        result = await search_service.search_summaries(
            q=q,
            user_id=user["user_id"],
            limit=limit,
            offset=offset,
            mode=mode,
            min_similarity=min_similarity,
            filters=filters,
        )
        return success_response(
            result,
            pagination=result.pagination,
        )
    except Exception as exc:
        logger.error("Search failed: %s", exc, exc_info=True)
        raise ProcessingError(f"Search failed: {exc!s}") from exc


@router.get("/search/semantic")
async def semantic_search_summaries(
    q: str = Query(..., min_length=2, max_length=200),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user_scope: str | None = Query(None, min_length=1, max_length=50),
    min_similarity: float = Query(0.2, ge=0.0, le=1.0),
    filters: SearchFilters = Depends(_search_filter_params),
    user: dict[str, Any] = Depends(get_current_user),
    search_service: SearchService = Depends(_get_search_service),
) -> Any:
    """Semantic search across summaries using Qdrant embeddings."""
    try:
        result = await search_service.semantic_search_summaries(
            q=q,
            user_id=user["user_id"],
            limit=limit,
            offset=offset,
            user_scope=user_scope,
            min_similarity=min_similarity,
            filters=filters,
        )
        return success_response(
            result,
            pagination=result.pagination,
        )
    except Exception as exc:
        logger.error("Semantic search failed: %s", exc, exc_info=True)
        raise ProcessingError(f"Semantic search failed: {exc!s}") from exc


@router.get("/topics/trending")
async def get_trending_topics(
    limit: int = Query(20, ge=1, le=100),
    days: int = Query(30, ge=1, le=365),
    user: dict[str, Any] = Depends(get_current_user),
) -> Any:
    """Get trending topic tags across recent summaries."""
    payload = await get_trending_payload(
        user["user_id"],
        limit=limit,
        days=days,
        database=get_session_manager(),
    )
    pagination = {
        "total": payload.get("total", limit),
        "limit": limit,
        "offset": 0,
        "has_more": False,
    }
    return success_response(payload, pagination=pagination)


@router.get("/search/insights")
async def get_search_insights(
    days: int = Query(30, ge=7, le=365),
    limit: int = Query(20, ge=5, le=100),
    user: dict[str, Any] = Depends(get_current_user),
    search_service: SearchService = Depends(_get_search_service),
) -> Any:
    """Search analytics snapshot: trends, entities, diversity, mix and coverage gaps."""
    payload, pagination = await search_service.get_search_insights(
        user_id=user["user_id"],
        days=days,
        limit=limit,
    )
    return success_response(payload, pagination=pagination)


@router.get("/topics/related")
async def get_related_summaries(
    tag: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
    search_service: SearchService = Depends(_get_search_service),
) -> Any:
    """Get summaries related to a specific topic tag."""
    payload = await search_service.get_related_summaries(
        user_id=user["user_id"],
        tag=tag,
        limit=limit,
        offset=offset,
    )
    return success_response(payload, pagination=payload["pagination"])


@router.get("/urls/check-duplicate")
async def check_duplicate(
    url: str = Query(..., min_length=10),
    include_summary: bool = Query(False),
    user: dict[str, Any] = Depends(get_current_user),
    search_service: SearchService = Depends(_get_search_service),
) -> Any:
    """Check if a URL has already been summarized."""
    payload = await search_service.check_duplicate(
        user_id=user["user_id"],
        url=url,
        include_summary=include_summary,
    )
    return success_response(payload)


def _get_repo_search_service(request: Request) -> Any:
    """Build RepositorySearchService from the app runtime."""
    from app.di.api import resolve_api_runtime
    from app.infrastructure.search.repository_search_service import RepositorySearchService

    runtime = resolve_api_runtime(request)
    db = get_session_manager(request)
    cfg = runtime.cfg

    qdrant = runtime.search.vector_store
    if qdrant is None:
        return None

    return RepositorySearchService(
        embedding_service=runtime.search.embedding_service,
        qdrant_store=qdrant,
        db=db,
        environment=cfg.vector_store.environment,
        user_scope=cfg.vector_store.user_scope,
    )


def _get_repo_correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", None) or str(uuid.uuid4())


@router.get("/search/repositories")
async def search_repositories(
    q: str = Query(..., min_length=2, max_length=200),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    min_similarity: float = Query(0.2, ge=0.0, le=1.0),
    languages: list[str] | None = Query(None),
    topics: list[str] | None = Query(None),
    is_starred: bool | None = Query(None),
    source: Literal["manual", "starred"] | None = Query(None),
    user: dict[str, Any] = Depends(get_current_user),
    service: Any = Depends(_get_repo_search_service),
    correlation_id: str = Depends(_get_repo_correlation_id),
) -> Any:
    """Semantic search over GitHub repositories."""
    from app.api.models.responses.common import PaginationInfo
    from app.api.models.responses.repositories import (
        RepositorySearchHit,
        RepositorySearchResponse,
    )

    if service is None:
        raise ProcessingError("Repository search is not available (Qdrant not configured)")

    try:
        results = await service.search(
            q,
            user_id=user["user_id"],
            languages=languages,
            topics=topics,
            is_starred=is_starred,
            source=source,
            limit=limit,
            offset=offset,
            min_similarity=min_similarity,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        logger.error("repository_search_failed: %s", exc, exc_info=True)
        raise ProcessingError(f"Repository search failed: {exc!s}") from exc

    hits = []
    for item in results.items:
        hits.append(
            RepositorySearchHit(
                id=item.repository_id,
                github_id=item.github_id,
                full_name=item.full_name,
                owner=item.owner,
                name=item.name,
                description=item.description,
                primary_language=item.primary_language,
                topics=item.topics,
                stars=item.stars,
                forks=0,
                is_starred=item.is_starred,
                is_archived=False,
                pushed_at=item.pushed_at,
                last_synced_at=item.pushed_at,  # best effort
                pending_analysis=False,
                has_analysis=False,
                source="manual",
                distance=item.distance,
            )
        )

    pagination = PaginationInfo(
        total=results.total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(hits)) < results.total,
    )
    response = RepositorySearchResponse(results=hits, pagination=pagination, query=q)
    return success_response(response, pagination=pagination)
