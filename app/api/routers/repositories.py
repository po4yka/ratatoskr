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
from app.api.models.responses.common import TypedSuccessResponse, success_response
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
    from app.application.use_cases.add_repository import AddRepositoryUseCase
    from app.application.use_cases.analyze_repository import AnalyzeRepositoryUseCase

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/repositories", tags=["repositories"])


# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------


def _get_db(request: Request) -> Database:
    from app.api.dependencies.database import get_session_manager

    return get_session_manager(request)


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


def _get_add_use_case(request: Request) -> AddRepositoryUseCase:
    """Resolve the pre-wired use case; the router stays transport-only."""
    from app.di.api import resolve_api_runtime

    use_case = resolve_api_runtime(request).add_repository_use_case
    if use_case is None:
        raise HTTPException(status_code=503, detail="Repository ingestion is not available")
    return cast("AddRepositoryUseCase", use_case)


def _translate_add_error(exc: Exception, *, url: str, correlation_id: str) -> HTTPException:
    """Map a domain error from the add flow onto its transport status."""
    from app.adapters.github.exceptions import (
        GitHubAuthError,
        GitHubIntegrationRequiredError,
        GitHubNotFoundError,
        GitHubRateLimitError,
    )
    from app.application.use_cases.add_repository import StarManagementUnavailableError

    if isinstance(exc, GitHubIntegrationRequiredError):
        return HTTPException(
            status_code=409,
            detail=(
                "GitHub integration required. Connect via /v1/auth/github/pat or "
                "/v1/auth/github/device/start."
            ),
        )
    if isinstance(exc, GitHubRateLimitError):
        return HTTPException(status_code=429, detail="GitHub rate limit exceeded")
    if isinstance(exc, GitHubAuthError):
        # Carries GitHub's own wording, which names the scope the token lacks.
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, GitHubNotFoundError):
        return HTTPException(status_code=404, detail="GitHub has no such repository")
    if isinstance(exc, StarManagementUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    logger.exception(
        "github_ingest_failed",
        extra={"url": url, "correlation_id": correlation_id},
    )
    return HTTPException(
        status_code=502,
        detail=f"GitHub ingestion failed (correlation_id={correlation_id})",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=TypedSuccessResponse[RepositoryListResponse])
async def list_repositories(
    is_starred: bool | None = Query(None),
    language: str | None = Query(None, max_length=100),
    topic: str | None = Query(None, max_length=100),
    list_name: str | None = Query(None, max_length=200, description="GitHub star list name"),
    source: Literal["manual", "starred"] | None = Query(None),
    pending_analysis: bool | None = Query(None),
    sort: RepositoryListSort = Query(RepositoryListSort.STARS_DESC),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
    svc: RepositoryService = Depends(_get_repository_service),
) -> dict[str, Any]:
    """List repositories for the authenticated user with optional filters."""
    result = await svc.list_repositories(
        user_id=user["user_id"],
        is_starred=is_starred,
        language=language,
        topic=topic,
        list_name=list_name,
        source=source,
        pending_analysis=pending_analysis,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return success_response(RepositoryListResponse.model_validate(result.model_dump()))


@router.get("/watched", response_model=TypedSuccessResponse[RepositoryWatchListResponse])
async def list_watched_repositories(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
    svc: RepositoryService = Depends(_get_repository_service),
) -> dict[str, Any]:
    """List repositories watched by the authenticated user."""
    result = await svc.list_repository_watches(
        user_id=user["user_id"],
        limit=limit,
        offset=offset,
    )
    return success_response(RepositoryWatchListResponse.model_validate(result.model_dump()))


@router.get("/{repository_id}", response_model=TypedSuccessResponse[RepositoryDetail])
async def get_repository(
    repository_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    svc: RepositoryService = Depends(_get_repository_service),
) -> dict[str, Any]:
    """Get full detail for a single repository."""
    try:
        result = await svc.get_repository(repository_id=repository_id, user_id=user["user_id"])
        return success_response(RepositoryDetail.model_validate(result.model_dump()))
    except RepositoryServiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Repository not found") from exc


@router.post("", response_model=TypedSuccessResponse[IngestRepositoryResponse], status_code=202)
async def ingest_repository(
    body: IngestRepositoryRequest,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: AddRepositoryUseCase = Depends(_get_add_use_case),
    correlation_id: str = Depends(_get_correlation_id),
) -> dict[str, Any]:
    """Add a GitHub repository by URL.

    ``mode=metadata`` (the default) only indexes it. ``mode=track`` also enrolls
    it for on-disk git backup without starring it. ``mode=star`` additionally
    stars it on GitHub and files it into one of the user's star lists.

    A star that lands while the list write or the backup enrollment fails is
    still a success: the response reports it through ``warnings`` rather than
    undoing the part that worked.
    """
    if not is_github_repo_url(body.url):
        raise HTTPException(status_code=400, detail="URL is not a github.com repository URL")

    try:
        result = await use_case.add(
            url=body.url,
            user_id=user["user_id"],
            mode=body.mode.value,
            list_names=body.list_names,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        raise _translate_add_error(exc, url=body.url, correlation_id=correlation_id) from exc

    return success_response(
        IngestRepositoryResponse(
            repository_id=result.repository_id,
            status="ready" if result.repository_id else "pending",
            full_name=result.full_name,
            mode=result.mode,
            is_starred=result.is_starred,
            lists_applied=result.lists_applied,
            list_suggestion_source=result.list_suggestion_source,
            mirror_id=result.mirror_id,
            warnings=result.warnings,
        )
    )


@router.post("/{repository_id}/watch", response_model=TypedSuccessResponse[RepositoryWatch])
async def watch_repository(
    repository_id: int,
    body: RepositoryWatchRequest | None = None,
    user: dict[str, Any] = Depends(get_current_user),
    svc: RepositoryService = Depends(_get_repository_service),
) -> dict[str, Any]:
    """Watch an owned repository for README and release deltas."""
    request_body = body or RepositoryWatchRequest()
    try:
        result = await svc.watch_repository(
            repository_id=repository_id,
            user_id=user["user_id"],
            watch_readme=request_body.watch_readme,
            watch_releases=request_body.watch_releases,
        )
        return success_response(RepositoryWatch.model_validate(result.model_dump()))
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


@router.post("/{repository_id}/reanalyze", response_model=TypedSuccessResponse[RepositoryDetail])
async def reanalyze_repository(
    repository_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: AnalyzeRepositoryUseCase = Depends(_get_analyze_use_case),
    correlation_id: str = Depends(_get_correlation_id),
    svc: RepositoryService = Depends(_get_repository_service),
) -> dict[str, Any]:
    """Force re-analysis of a repository."""
    try:
        result = await svc.reanalyze_repository(
            repository_id=repository_id,
            user_id=user["user_id"],
            use_case=use_case,
            correlation_id=correlation_id,
        )
        return success_response(RepositoryDetail.model_validate(result.model_dump()))
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
