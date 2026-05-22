"""GitHub repository management endpoints (US-028, US-029)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select

from app.adapters.github.url_patterns import is_github_repo_url
from app.api.models.requests import IngestRepositoryRequest, RepositoryListSort
from app.api.models.responses.common import PaginationInfo
from app.api.models.responses.repositories import (
    IngestRepositoryResponse,
    RepositoryAnalysis,
    RepositoryCompact,
    RepositoryDetail,
    RepositoryListResponse,
)
from app.api.routers.auth import get_current_user
from app.core.logging_utils import get_logger
from app.db.models.repository import Repository
from app.db.session import (  # noqa: TC001  # used at runtime in FastAPI Depends() signatures
    Database,
)

if TYPE_CHECKING:
    from app.adapters.github.platform_extractor import GitHubPlatformExtractor
    from app.application.use_cases.analyze_repository import AnalyzeRepositoryUseCase
    from app.infrastructure.vector.qdrant_store import QdrantVectorStore

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/repositories", tags=["repositories"])


# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------


def _get_db(request: Request) -> Database:
    from app.api.dependencies.database import get_session_manager

    return get_session_manager(request)


def _get_github_extractor(request: Request) -> GitHubPlatformExtractor:
    """Build GitHubPlatformExtractor from the app runtime."""
    from app.adapters.github.platform_extractor import GitHubPlatformExtractor
    from app.agents.repo_analysis_agent import RepoAnalysisAgent
    from app.application.use_cases.analyze_repository import AnalyzeRepositoryUseCase
    from app.di.api import resolve_api_runtime

    db = _get_db(request)
    runtime = resolve_api_runtime(request)
    cfg = runtime.cfg

    embedding_gen = _build_repo_embedding_gen(request)
    agent = RepoAnalysisAgent(llm_service=runtime.core.llm_client)
    analyze_use_case = AnalyzeRepositoryUseCase(
        db=db,
        agent=agent,
        embedding_gen=embedding_gen,
    )
    return GitHubPlatformExtractor(
        db=db,
        github_config=cfg.github,
        analyze_use_case=analyze_use_case,
    )


def _get_analyze_use_case(request: Request) -> AnalyzeRepositoryUseCase:
    """Build AnalyzeRepositoryUseCase from the app runtime."""
    from app.agents.repo_analysis_agent import RepoAnalysisAgent
    from app.application.use_cases.analyze_repository import AnalyzeRepositoryUseCase
    from app.di.api import resolve_api_runtime

    db = _get_db(request)
    runtime = resolve_api_runtime(request)
    embedding_gen = _build_repo_embedding_gen(request)
    agent = RepoAnalysisAgent(llm_service=runtime.core.llm_client)
    return AnalyzeRepositoryUseCase(
        db=db,
        agent=agent,
        embedding_gen=embedding_gen,
    )


def _build_repo_embedding_gen(request: Request) -> Any:
    """Build RepositoryEmbeddingGenerator from the app runtime."""
    from app.di.api import resolve_api_runtime
    from app.infrastructure.embedding.repository_embedding import RepositoryEmbeddingGenerator

    runtime = resolve_api_runtime(request)
    cfg = runtime.cfg
    db = _get_db(request)
    qdrant = runtime.search.vector_store  # may be None
    return RepositoryEmbeddingGenerator(
        embedding_service=runtime.search.embedding_service,
        qdrant_store=qdrant,
        db=db,
        environment=cfg.vector_store.environment,
        user_scope=cfg.vector_store.user_scope,
    )


def _get_qdrant(request: Request) -> QdrantVectorStore | None:
    from app.di.api import resolve_api_runtime

    runtime = resolve_api_runtime(request)
    return runtime.search.vector_store


def _get_correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", None) or str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sort_clause(sort: RepositoryListSort) -> list[Any]:
    from sqlalchemy import asc, desc, nulls_last

    if sort == RepositoryListSort.STARS_DESC:
        return [desc(Repository.stars), desc(Repository.pushed_at)]
    if sort == RepositoryListSort.PUSHED_DESC:
        return [nulls_last(desc(Repository.pushed_at))]
    if sort == RepositoryListSort.CREATED_DESC:
        return [desc(Repository.created_at)]
    # FULL_NAME_ASC
    return [asc(Repository.full_name)]


def _repo_to_compact(row: Repository) -> RepositoryCompact:
    topics: list[str] = list(row.topics_json) if isinstance(row.topics_json, list) else []
    return RepositoryCompact(
        id=row.id,
        github_id=row.github_id,
        full_name=row.full_name,
        owner=row.owner,
        name=row.name,
        description=row.description,
        primary_language=row.primary_language,
        topics=topics,
        stars=row.stars,
        forks=row.forks,
        is_starred=row.is_starred,
        is_archived=row.is_archived,
        pushed_at=row.pushed_at,
        last_synced_at=row.last_synced_at,
        pending_analysis=row.pending_analysis,
        has_analysis=row.analysis_json is not None,
        source=row.source.value if hasattr(row.source, "value") else str(row.source),
    )


def _repo_to_detail(row: Repository) -> RepositoryDetail:
    topics: list[str] = list(row.topics_json) if isinstance(row.topics_json, list) else []
    languages: dict[str, int] = (
        dict(row.languages_json) if isinstance(row.languages_json, dict) else {}
    )
    analysis: RepositoryAnalysis | None = None
    if row.analysis_json is not None:
        try:
            a = row.analysis_json
            analysis = RepositoryAnalysis(
                purpose=a.get("purpose", ""),
                tech_stack=a.get("tech_stack", []),
                architecture_summary=a.get("architecture_summary", ""),
                key_concepts=[
                    kc if isinstance(kc, dict) else kc.model_dump()
                    for kc in a.get("key_concepts", [])
                ],
                code_patterns=[
                    cp if isinstance(cp, dict) else cp.model_dump()
                    for cp in a.get("code_patterns", [])
                ],
                use_cases=a.get("use_cases", []),
                target_audience=a.get("target_audience", ""),
                maturity=a.get("maturity", ""),
                key_dependencies=a.get("key_dependencies", []),
                hallucination_risk=a.get("hallucination_risk", ""),
                confidence=a.get("confidence", 0.0),
            )
        except Exception:
            analysis = None

    return RepositoryDetail(
        id=row.id,
        github_id=row.github_id,
        full_name=row.full_name,
        owner=row.owner,
        name=row.name,
        description=row.description,
        primary_language=row.primary_language,
        topics=topics,
        stars=row.stars,
        forks=row.forks,
        is_starred=row.is_starred,
        is_archived=row.is_archived,
        pushed_at=row.pushed_at,
        last_synced_at=row.last_synced_at,
        pending_analysis=row.pending_analysis,
        has_analysis=row.analysis_json is not None,
        source=row.source.value if hasattr(row.source, "value") else str(row.source),
        homepage_url=row.homepage_url,
        license_spdx=row.license_spdx,
        is_fork=row.is_fork,
        is_template=row.is_template,
        languages=languages,
        readme_excerpt=row.readme_excerpt,
        analysis=analysis,
        analysis_model=row.analysis_model,
        analysis_at=row.analysis_at,
        content_hash=row.content_hash,
        created_at_github=row.created_at_github,
        watchers=row.watchers,
    )


async def _load_owned_repository(
    db: Database,
    *,
    repository_id: int,
    user_id: int,
) -> Repository | None:
    """Load a repository only when it belongs to the authenticated user."""
    async with db.session() as session:
        stmt = select(Repository).where(
            Repository.id == repository_id,
            Repository.user_id == user_id,
        )
        return (await session.execute(stmt)).scalar_one_or_none()


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
    db: Database = Depends(_get_db),
) -> RepositoryListResponse:
    """List repositories for the authenticated user with optional filters."""
    user_id: int = user["user_id"]

    async with db.session() as session:
        # Base filter
        conditions = [Repository.user_id == user_id]
        if is_starred is not None:
            conditions.append(Repository.is_starred == is_starred)
        if language is not None:
            conditions.append(Repository.primary_language == language)
        if topic is not None:
            from sqlalchemy import cast
            from sqlalchemy.dialects.postgresql import JSONB

            conditions.append(Repository.topics_json.contains(cast([topic], JSONB)))
        if source is not None:
            conditions.append(Repository.source == source)
        if pending_analysis is not None:
            conditions.append(Repository.pending_analysis == pending_analysis)

        # COUNT
        count_stmt = select(func.count()).select_from(Repository).where(*conditions)
        total: int = int(await session.scalar(count_stmt) or 0)

        # Rows
        order_by = _sort_clause(sort)
        rows_stmt = (
            select(Repository).where(*conditions).order_by(*order_by).limit(limit).offset(offset)
        )
        result = await session.execute(rows_stmt)
        rows = list(result.scalars().all())

    repos = [_repo_to_compact(r) for r in rows]
    pagination = PaginationInfo(
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(rows)) < total,
    )
    return RepositoryListResponse(repositories=repos, pagination=pagination)


@router.get("/{repository_id}", response_model=RepositoryDetail)
async def get_repository(
    repository_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(_get_db),
) -> RepositoryDetail:
    """Get full detail for a single repository."""
    user_id: int = user["user_id"]

    row = await _load_owned_repository(db, repository_id=repository_id, user_id=user_id)

    if row is None:
        raise HTTPException(status_code=404, detail="Repository not found")

    return _repo_to_detail(row)


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


@router.post("/{repository_id}/reanalyze", response_model=RepositoryDetail)
async def reanalyze_repository(
    repository_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: AnalyzeRepositoryUseCase = Depends(_get_analyze_use_case),
    correlation_id: str = Depends(_get_correlation_id),
    db: Database = Depends(_get_db),
) -> RepositoryDetail:
    """Force re-analysis of a repository."""
    user_id: int = user["user_id"]

    # Verify ownership first.
    row = await _load_owned_repository(db, repository_id=repository_id, user_id=user_id)

    if row is None:
        raise HTTPException(status_code=404, detail="Repository not found")

    from app.application.use_cases.analyze_repository import RepositoryNotFoundError

    try:
        await use_case.analyze(
            repository_id,
            force=True,
            correlation_id=correlation_id,
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Repository not found") from exc

    # Reload under the same ownership predicate so a stale or reused repository
    # ID cannot cross user boundaries after analysis completes.
    updated_row = await _load_owned_repository(db, repository_id=repository_id, user_id=user_id)

    if updated_row is None:
        raise HTTPException(status_code=404, detail="Repository not found")

    return _repo_to_detail(updated_row)


@router.delete("/{repository_id}", status_code=204)
async def delete_repository(
    repository_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(_get_db),
    qdrant: QdrantVectorStore | None = Depends(_get_qdrant),
) -> None:
    """Delete a repository and its Qdrant embedding point."""
    import uuid as _uuid

    from sqlalchemy import delete as sql_delete

    from app.db.models.repository import RepositoryEmbedding

    user_id: int = user["user_id"]

    row = await _load_owned_repository(db, repository_id=repository_id, user_id=user_id)

    if row is None:
        raise HTTPException(status_code=404, detail="Repository not found")

    # Delete Qdrant point using the same key scheme as RepositoryEmbeddingGenerator
    if qdrant is not None and qdrant.available:
        env = getattr(qdrant, "_environment", "")
        scope = getattr(qdrant, "_user_scope", "")
        point_key = f"{env}:{scope}:repository:{repository_id}"
        point_id = str(_uuid.uuid5(_uuid.NAMESPACE_OID, point_key))

        try:
            import asyncio

            from qdrant_client.models import PointIdsList

            await asyncio.to_thread(
                qdrant._client.delete,
                qdrant._collection_name,
                points_selector=PointIdsList(points=[point_id]),
            )
        except Exception as exc:
            logger.warning(
                "delete_repository_qdrant_failed",
                extra={"repository_id": repository_id, "error": str(exc)},
            )

    # Delete DB rows (RepositoryEmbedding cascades via FK, but delete explicitly for safety)
    async with db.transaction() as session:
        await session.execute(
            sql_delete(RepositoryEmbedding).where(
                RepositoryEmbedding.repository_id == repository_id
            )
        )
        await session.execute(sql_delete(Repository).where(Repository.id == repository_id))
