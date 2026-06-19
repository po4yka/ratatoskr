"""GitHub repository management endpoints (US-028, US-029)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.adapters.github.url_patterns import is_github_repo_url
from app.api.models.requests import (
    IngestRepositoryRequest,
    RepositoryListSort,
    RepositoryWatchRequest,
)
from app.api.models.responses.repositories import (
    IngestRepositoryResponse,
    RepositoryDetail,
    RepositoryListResponse,
    RepositoryWatch,
    RepositoryWatchListResponse,
)
from app.api.routers.auth import get_current_user
from app.application.services.repository_service import (
    RepositoryService,
    RepositoryServiceNotFoundError,
)
from app.core.logging_utils import get_logger
from app.db.session import (  # noqa: TC001  # used at runtime in FastAPI Depends() signatures
    Database,
)

if TYPE_CHECKING:
    from app.adapters.github.platform_extractor import GitHubPlatformExtractor
    from app.application.use_cases.analyze_repository import AnalyzeRepositoryUseCase

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/repositories", tags=["repositories"])


# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------


def _get_db(request: Request) -> Database:
    from app.api.dependencies.database import get_session_manager

    return get_session_manager(request)


def _get_github_extractor(request: Request) -> GitHubPlatformExtractor:
    from app.di.api import resolve_api_runtime

    return cast("GitHubPlatformExtractor", resolve_api_runtime(request).github_platform_extractor)


def _get_analyze_use_case(request: Request) -> AnalyzeRepositoryUseCase:
    from app.di.api import resolve_api_runtime

    return cast(
        "AnalyzeRepositoryUseCase", resolve_api_runtime(request).analyze_repository_use_case
    )


def _get_repository_service(request: Request) -> RepositoryService:
    from app.di.api import resolve_api_runtime
    from app.infrastructure.persistence.repositories.repository_read_repository import (
        RepositoryReadRepositoryAdapter,
    )

    try:
        return cast("RepositoryService", resolve_api_runtime(request).repository_service)
    except RuntimeError:
        return RepositoryService(repository_repo=RepositoryReadRepositoryAdapter(_get_db(request)))


def _get_qdrant(request: Request) -> Any:
    """Compatibility shim for older tests that override this dependency."""
    from app.di.api import resolve_api_runtime

    return resolve_api_runtime(request).search.vector_store


def _get_correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", None) or str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=RepositoryListResponse)
async def list_repositories(
    is_starred: bool | None = Query(None),
    language: str | None = Query(None, max_length=100),
    topic: str | None = Query(None, max_length=100),
    source: Literal["manual", "starred"] | None = Query(None),
    pending_analysis: bool | None = Query(None),
    sort: RepositoryListSort = Query(RepositoryListSort.STARS_DESC),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
    svc: RepositoryService = Depends(_get_repository_service),
) -> RepositoryListResponse:
    """List repositories for the authenticated user with optional filters."""
    result = await svc.list_repositories(
        user_id=user["user_id"],
        is_starred=is_starred,
        language=language,
        topic=topic,
        source=source,
        pending_analysis=pending_analysis,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return RepositoryListResponse.model_validate(result.model_dump())


@router.get("/watched", response_model=RepositoryWatchListResponse)
async def list_watched_repositories(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
    svc: RepositoryService = Depends(_get_repository_service),
) -> RepositoryWatchListResponse:
    """List repositories watched by the authenticated user."""
    result = await svc.list_repository_watches(
        user_id=user["user_id"],
        limit=limit,
        offset=offset,
    )
    return RepositoryWatchListResponse.model_validate(result.model_dump())


@router.get("/{repository_id}", response_model=RepositoryDetail)
async def get_repository(
    repository_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    svc: RepositoryService = Depends(_get_repository_service),
) -> RepositoryDetail:
    """Get full detail for a single repository."""
    try:
        result = await svc.get_repository(repository_id=repository_id, user_id=user["user_id"])
        return RepositoryDetail.model_validate(result.model_dump())
    except RepositoryServiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Repository not found") from exc


@router.post("", response_model=IngestRepositoryResponse, status_code=202)
async def ingest_repository(
    body: IngestRepositoryRequest,
    user: dict[str, Any] = Depends(get_current_user),
    extractor: GitHubPlatformExtractor = Depends(_get_github_extractor),
    correlation_id: str = Depends(_get_correlation_id),
) -> IngestRepositoryResponse:
    """Ingest a GitHub repository by URL."""
    if not is_github_repo_url(body.url):
        raise HTTPException(status_code=400, detail="URL is not a github.com repository URL")

    from app.adapters.content.platform_extraction.models import PlatformExtractionRequest

    request_envelope = PlatformExtractionRequest(
        message=None,
        url_text=body.url,
        normalized_url=body.url,
        correlation_id=correlation_id,
        user_id=user["user_id"],
        mode="pure",
    )

    from app.adapters.github.exceptions import (
        GitHubAuthError,
        GitHubIntegrationRequiredError,
        GitHubNotFoundError,
    )

    try:
        result = await extractor.extract(request_envelope)
    except GitHubIntegrationRequiredError as exc:
        raise HTTPException(
            status_code=400,
            detail="GitHub integration required. Connect via /v1/auth/github/pat or /v1/auth/github/device/start.",
        ) from exc
    except (GitHubAuthError, GitHubNotFoundError) as exc:
        raise HTTPException(
            status_code=502,
            detail="GitHub returned an error fetching the repository",
        ) from exc
    except Exception as exc:
        logger.exception(
            "github_ingest_failed",
            extra={"url": body.url, "correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=502,
            detail=f"GitHub ingestion failed (correlation_id={correlation_id})",
        ) from exc

    metadata = result.metadata or {}
    full_name: str = metadata.get("full_name") or result.title or body.url
    repository_id: int = result.request_id or 0

    return IngestRepositoryResponse(
        repository_id=repository_id,
        status="ready" if repository_id else "pending",
        full_name=full_name,
    )


@router.post("/{repository_id}/watch", response_model=RepositoryWatch)
async def watch_repository(
    repository_id: int,
    body: RepositoryWatchRequest | None = None,
    user: dict[str, Any] = Depends(get_current_user),
    svc: RepositoryService = Depends(_get_repository_service),
) -> RepositoryWatch:
    """Watch an owned repository for README and release deltas."""
    request_body = body or RepositoryWatchRequest()
    try:
        result = await svc.watch_repository(
            repository_id=repository_id,
            user_id=user["user_id"],
            watch_readme=request_body.watch_readme,
            watch_releases=request_body.watch_releases,
        )
        return RepositoryWatch.model_validate(result.model_dump())
    except RepositoryServiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Repository not found") from exc


@router.delete("/{repository_id}/watch", status_code=204)
async def unwatch_repository(
    repository_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    svc: RepositoryService = Depends(_get_repository_service),
) -> None:
    """Remove a repository watch owned by the authenticated user."""
    try:
        await svc.unwatch_repository(repository_id=repository_id, user_id=user["user_id"])
    except RepositoryServiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Repository watch not found") from exc


@router.post("/{repository_id}/reanalyze", response_model=RepositoryDetail)
async def reanalyze_repository(
    repository_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: AnalyzeRepositoryUseCase = Depends(_get_analyze_use_case),
    correlation_id: str = Depends(_get_correlation_id),
    svc: RepositoryService = Depends(_get_repository_service),
) -> RepositoryDetail:
    """Force re-analysis of a repository."""
    try:
        result = await svc.reanalyze_repository(
            repository_id=repository_id,
            user_id=user["user_id"],
            use_case=use_case,
            correlation_id=correlation_id,
        )
        return RepositoryDetail.model_validate(result.model_dump())
    except RepositoryServiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Repository not found") from exc


async def delete_repository(
    repository_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    svc: RepositoryService = Depends(_get_repository_service),
) -> None:
    """Delete a repository and its Qdrant embedding point."""
    try:
        await svc.delete_repository(repository_id=repository_id, user_id=user["user_id"])
    except RepositoryServiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Repository not found") from exc


router.add_api_route(
    "/{repository_id}",
    delete_repository,
    methods=["DELETE"],
    status_code=204,
)
