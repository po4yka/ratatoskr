"""Search and discovery endpoints."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from app.api.dependencies.database import get_search_read_model_use_case, get_session_manager
from app.api.exceptions import ProcessingError, ResourceNotFoundError
from app.api.models.responses import success_response
from app.api.routers.auth import get_current_user
from app.api.search_helpers import SearchFilters
from app.api.services.search_service import SearchService
from app.core.logging_utils import get_logger
from app.db.models import SavedSearch, SearchHistoryEntry, User
from app.infrastructure.cache.trending_cache import get_trending_payload

if TYPE_CHECKING:
    from app.application.use_cases.search_read_model import SearchReadModelUseCase

logger = get_logger(__name__)
router = APIRouter()
_SEARCH_HISTORY_LIMIT = 50


class SavedSearchCreateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=120)
    query: str = Field(..., min_length=2, max_length=200)
    mode: Literal["auto", "keyword", "semantic", "hybrid"] = "auto"
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    min_similarity: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        validation_alias="minSimilarity",
        serialization_alias="minSimilarity",
    )
    language: str | None = Field(default=None, min_length=2, max_length=10)
    tags: list[str] | None = None
    domains: list[str] | None = None
    start_date: str | None = Field(
        default=None, validation_alias="startDate", serialization_alias="startDate"
    )
    end_date: str | None = Field(
        default=None, validation_alias="endDate", serialization_alias="endDate"
    )
    is_read: bool | None = Field(
        default=None, validation_alias="isRead", serialization_alias="isRead"
    )
    is_favorited: bool | None = Field(
        default=None, validation_alias="isFavorited", serialization_alias="isFavorited"
    )


class SavedSearchResponse(BaseModel):
    id: int
    name: str
    query: str
    filters: dict[str, Any]
    created_at: str = Field(serialization_alias="createdAt")


class SearchHistoryEntryResponse(BaseModel):
    id: int
    query: str
    filters: dict[str, Any]
    created_at: str = Field(serialization_alias="createdAt")


class SearchHistoryListResponse(BaseModel):
    entries: list[SearchHistoryEntryResponse]
    enabled: bool


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


def _filters_to_payload(
    *,
    mode: str,
    limit: int,
    offset: int,
    min_similarity: float,
    filters: SearchFilters,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "limit": limit,
        "offset": offset,
        "min_similarity": min_similarity,
        "language": filters.language,
        "tags": filters.tags,
        "domains": filters.domains,
        "start_date": filters.start_date,
        "end_date": filters.end_date,
        "is_read": filters.is_read,
        "is_favorited": filters.is_favorited,
    }


def _filters_from_payload(payload: dict[str, Any]) -> SearchFilters:
    return SearchFilters(
        language=_optional_str(payload.get("language")),
        tags=_optional_str_list(payload.get("tags")),
        domains=_optional_str_list(payload.get("domains")),
        start_date=_optional_str(payload.get("start_date")),
        end_date=_optional_str(payload.get("end_date")),
        is_read=_optional_bool(payload.get("is_read")),
        is_favorited=_optional_bool(payload.get("is_favorited")),
    )


def _saved_create_to_filters_payload(body: SavedSearchCreateRequest) -> dict[str, Any]:
    return {
        "mode": body.mode,
        "limit": body.limit,
        "offset": body.offset,
        "min_similarity": body.min_similarity,
        "language": body.language,
        "tags": body.tags,
        "domains": body.domains,
        "start_date": body.start_date,
        "end_date": body.end_date,
        "is_read": body.is_read,
        "is_favorited": body.is_favorited,
    }


def _saved_search_response(row: SavedSearch) -> SavedSearchResponse:
    return SavedSearchResponse(
        id=row.id,
        name=row.name,
        query=row.query,
        filters=_coerce_filters_json(row.filters_json),
        created_at=row.created_at.isoformat(),
    )


def _history_response(row: SearchHistoryEntry) -> SearchHistoryEntryResponse:
    return SearchHistoryEntryResponse(
        id=row.id,
        query=row.query,
        filters=_coerce_filters_json(row.filters_json),
        created_at=row.created_at.isoformat(),
    )


def _coerce_filters_json(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result = [item for item in value if isinstance(item, str) and item]
    return result or None


def _history_enabled(user_record: User | None) -> bool:
    prefs = user_record.preferences_json if user_record is not None else None
    return isinstance(prefs, dict) and prefs.get("search_history_enabled") is True


async def _record_search_history_if_enabled(
    *,
    user_id: int,
    q: str,
    filters_payload: dict[str, Any],
) -> None:
    db = get_session_manager()
    async with db.transaction() as session:
        user_record = await session.get(User, user_id)
        if not _history_enabled(user_record):
            return
        session.add(
            SearchHistoryEntry(
                user_id=user_id,
                query=q,
                filters_json=filters_payload,
            )
        )
        await session.flush()
        stale_ids = (
            await session.execute(
                select(SearchHistoryEntry.id)
                .where(SearchHistoryEntry.user_id == user_id)
                .order_by(SearchHistoryEntry.created_at.desc(), SearchHistoryEntry.id.desc())
                .offset(_SEARCH_HISTORY_LIMIT)
            )
        ).scalars()
        stale = list(stale_ids)
        if stale:
            await session.execute(
                delete(SearchHistoryEntry).where(SearchHistoryEntry.id.in_(stale))
            )


async def _run_search_with_payload(
    *,
    q: str,
    user_id: int,
    filters_payload: dict[str, Any],
    search_service: SearchService,
) -> Any:
    mode = str(filters_payload.get("mode") or "auto")
    if mode not in {"auto", "keyword", "semantic", "hybrid"}:
        mode = "auto"
    limit = _int_in_range(filters_payload.get("limit"), default=20, minimum=1, maximum=100)
    offset = _int_in_range(filters_payload.get("offset"), default=0, minimum=0, maximum=100_000)
    min_similarity = _float_in_range(
        filters_payload.get("min_similarity"), default=0.2, minimum=0.0, maximum=1.0
    )
    return await search_service.search_summaries(
        q=q,
        user_id=user_id,
        limit=limit,
        offset=offset,
        mode=mode,
        min_similarity=min_similarity,
        filters=_filters_from_payload(filters_payload),
    )


def _int_in_range(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if not isinstance(value, int):
        return default
    return max(minimum, min(maximum, value))


def _float_in_range(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    if not isinstance(value, int | float):
        return default
    return max(minimum, min(maximum, float(value)))


@router.get("/searches/saved")
async def list_saved_searches(
    user: dict[str, Any] = Depends(get_current_user),
) -> Any:
    """List the current user's saved searches."""
    db = get_session_manager()
    async with db.session() as session:
        rows = (
            await session.execute(
                select(SavedSearch)
                .where(SavedSearch.user_id == user["user_id"])
                .order_by(SavedSearch.created_at.desc(), SavedSearch.id.desc())
            )
        ).scalars()
        return success_response(
            {
                "saved_searches": [
                    _saved_search_response(row).model_dump(by_alias=True) for row in rows
                ]
            }
        )


@router.post("/searches/saved", status_code=status.HTTP_201_CREATED)
async def create_saved_search(
    body: SavedSearchCreateRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> Any:
    """Save a reusable query and filter bundle."""
    db = get_session_manager()
    async with db.transaction() as session:
        row = SavedSearch(
            user_id=user["user_id"],
            name=body.name.strip(),
            query=body.query.strip(),
            filters_json=_saved_create_to_filters_payload(body),
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return success_response(_saved_search_response(row))


@router.delete("/searches/saved/{saved_search_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_search(
    saved_search_id: int = Path(..., ge=1),
    user: dict[str, Any] = Depends(get_current_user),
) -> None:
    """Delete one saved search owned by the current user."""
    db = get_session_manager()
    async with db.transaction() as session:
        row = await session.get(SavedSearch, saved_search_id)
        if row is None or row.user_id != user["user_id"]:
            raise ResourceNotFoundError("SavedSearch", saved_search_id)
        await session.delete(row)


@router.post("/searches/saved/{saved_search_id}/run")
async def run_saved_search(
    saved_search_id: int = Path(..., ge=1),
    user: dict[str, Any] = Depends(get_current_user),
    search_service: SearchService = Depends(_get_search_service),
) -> Any:
    """Run a saved search through the same path as the direct search endpoint."""
    db = get_session_manager()
    async with db.session() as session:
        row = await session.get(SavedSearch, saved_search_id)
        if row is None or row.user_id != user["user_id"]:
            raise ResourceNotFoundError("SavedSearch", saved_search_id)
        query = row.query
        filters_payload = _coerce_filters_json(row.filters_json)

    try:
        result = await _run_search_with_payload(
            q=query,
            user_id=user["user_id"],
            filters_payload=filters_payload,
            search_service=search_service,
        )
        await _record_search_history_if_enabled(
            user_id=user["user_id"],
            q=query,
            filters_payload=filters_payload,
        )
        return success_response(result, pagination=result.pagination)
    except Exception as exc:
        logger.error("Saved search run failed: %s", exc, exc_info=True)
        raise ProcessingError(f"Saved search run failed: {exc!s}") from exc


@router.get("/searches/history")
async def list_search_history(
    user: dict[str, Any] = Depends(get_current_user),
) -> Any:
    """List recent search history entries when the user has opted in."""
    db = get_session_manager()
    async with db.session() as session:
        user_record = await session.get(User, user["user_id"])
        if not _history_enabled(user_record):
            return success_response(SearchHistoryListResponse(entries=[], enabled=False))
        rows = (
            await session.execute(
                select(SearchHistoryEntry)
                .where(SearchHistoryEntry.user_id == user["user_id"])
                .order_by(SearchHistoryEntry.created_at.desc(), SearchHistoryEntry.id.desc())
                .limit(_SEARCH_HISTORY_LIMIT)
            )
        ).scalars()
        return success_response(
            SearchHistoryListResponse(
                entries=[_history_response(row) for row in rows],
                enabled=True,
            )
        )


@router.delete("/searches/history")
async def clear_search_history(
    user: dict[str, Any] = Depends(get_current_user),
) -> Any:
    """Clear all search history entries for the current user."""
    db = get_session_manager()
    async with db.transaction() as session:
        await session.execute(
            delete(SearchHistoryEntry).where(SearchHistoryEntry.user_id == user["user_id"])
        )
    return success_response({"cleared": True})


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
        await _record_search_history_if_enabled(
            user_id=user["user_id"],
            q=q,
            filters_payload=_filters_to_payload(
                mode=mode,
                limit=limit,
                offset=offset,
                min_similarity=min_similarity,
                filters=filters,
            ),
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
