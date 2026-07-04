"""Git mirror management endpoints."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import delete as sql_delete, func, select

from app.adapters.github.url_patterns import parse_github_repo_url
from app.api.models.requests import (  # noqa: TC001  # used at runtime by FastAPI body schema
    RegisterMirrorRequest,
)
from app.api.models.responses.common import PaginationInfo
from app.api.models.responses.git_mirrors import (
    GitMirrorCompact,
    GitMirrorDetail,
    GitMirrorListResponse,
    GitMirrorSearchItem,
    GitMirrorSearchResponse,
    RegisterMirrorResponse,
)
from app.api.routers.auth import get_current_user
from app.core.git_url_safety import assert_safe_git_url, is_github_host
from app.core.logging_utils import get_logger
from app.db.models.git_backup import GitMirror, GitMirrorSource
from app.db.models.repository import Repository
from app.db.session import (  # noqa: TC001  # used at runtime in FastAPI Depends() signatures
    Database,
)

if TYPE_CHECKING:
    from app.adapters.git_backup.repository import GitMirrorRepository
    from app.config import AppConfig
    from app.config.git_backup import GitBackupConfig

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/git-mirrors", tags=["git-mirrors"])


# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------


def _get_db(request: Request) -> Database:
    from app.api.dependencies.database import get_session_manager

    return get_session_manager(request)


def _get_app_config(request: Request) -> AppConfig:
    from app.di.api import resolve_api_runtime

    return resolve_api_runtime(request).cfg


def _get_git_backup_config(request: Request) -> GitBackupConfig:
    return _get_app_config(request).git_backup


def _get_mirror_repo(request: Request) -> GitMirrorRepository:
    from app.adapters.git_backup.repository import GitMirrorRepository

    db = _get_db(request)
    cfg = _get_git_backup_config(request)
    return GitMirrorRepository(db=db, config=cfg)


def _get_correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", None) or str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mirror_to_compact(row: GitMirror) -> GitMirrorCompact:
    return GitMirrorCompact(
        id=row.id,
        clone_url=row.clone_url,
        name=row.name,
        status=row.status.value if hasattr(row.status, "value") else str(row.status),
        source=row.source.value if hasattr(row.source, "value") else str(row.source),
        last_mirrored_at=row.last_mirrored_at,
        size_kb=row.size_kb,
        repository_id=row.repository_id,
    )


def _mirror_to_detail(row: GitMirror) -> GitMirrorDetail:
    return GitMirrorDetail(
        id=row.id,
        clone_url=row.clone_url,
        name=row.name,
        status=row.status.value if hasattr(row.status, "value") else str(row.status),
        source=row.source.value if hasattr(row.source, "value") else str(row.source),
        last_mirrored_at=row.last_mirrored_at,
        size_kb=row.size_kb,
        repository_id=row.repository_id,
        mirror_path=row.mirror_path,
        default_branch=row.default_branch,
        consecutive_failures=row.consecutive_failures,
        last_error=row.last_error,
        last_error_category=row.last_error_category,
        backoff_until=row.backoff_until,
        last_attempt_at=row.last_attempt_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _load_owned_mirror(
    db: Database,
    *,
    mirror_id: int,
    user_id: int,
) -> GitMirror | None:
    """Load a mirror only when it belongs to the authenticated user."""
    async with db.session() as session:
        stmt = select(GitMirror).where(
            GitMirror.id == mirror_id,
            GitMirror.user_id == user_id,
        )
        return (await session.execute(stmt)).scalar_one_or_none()


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


def _validate_repository_mirror_target(
    *,
    repository: Repository,
    clone_url: str,
) -> None:
    parsed = parse_github_repo_url(clone_url)
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail="repository_id can only be attached to a matching GitHub repository URL",
        )

    owner, name = parsed
    expected_owner, expected_name = repository.full_name.split("/", 1)
    if (owner.casefold(), name.casefold()) != (expected_owner.casefold(), expected_name.casefold()):
        raise HTTPException(
            status_code=400,
            detail="clone_url does not match repository_id",
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=GitMirrorListResponse)
async def list_mirrors(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(_get_db),
) -> GitMirrorListResponse:
    """List git mirrors for the authenticated user with simple paging."""
    user_id: int = user["user_id"]

    async with db.session() as session:
        count_stmt = select(func.count()).select_from(GitMirror).where(GitMirror.user_id == user_id)
        total: int = int(await session.scalar(count_stmt) or 0)

        rows_stmt = (
            select(GitMirror)
            .where(GitMirror.user_id == user_id)
            .order_by(GitMirror.id)
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(rows_stmt)
        rows = list(result.scalars().all())

    mirrors = [_mirror_to_compact(r) for r in rows]
    pagination = PaginationInfo(
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(rows)) < total,
    )
    return GitMirrorListResponse(mirrors=mirrors, pagination=pagination)


@router.post("", response_model=RegisterMirrorResponse, status_code=202)
async def register_mirror(
    body: RegisterMirrorRequest,
    user: dict[str, Any] = Depends(get_current_user),
    mirror_repo: GitMirrorRepository = Depends(_get_mirror_repo),
    db: Database = Depends(_get_db),
    correlation_id: str = Depends(_get_correlation_id),
) -> RegisterMirrorResponse:
    """Register a git URL as a mirror target (upsert) and schedule it for cloning.

    Returns 202 Accepted. The actual clone/fetch happens in the next Taskiq
    git-backup sync job run. On-disk data is not created here.
    """
    user_id: int = user["user_id"]

    # Syntactic SSRF guard: reject literal-IP and blocked-hostname targets before
    # any DB write. The authoritative resolution-time check (assert_resolved_public_host)
    # runs inside the git-backup worker immediately before the actual clone.
    clone_url = body.clone_url
    try:
        assert_safe_git_url(clone_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Classify by the URL's real parsed host (not a substring match): a userinfo
    # or lookalike host like github.com@evil.com / github.com.evil.com must NOT be
    # treated as GitHub, or _resolve_url would embed the user's token for it.
    source = GitMirrorSource.GITHUB if is_github_host(clone_url) else GitMirrorSource.MANUAL
    if body.repository_id is not None:
        repository = await _load_owned_repository(
            db,
            repository_id=body.repository_id,
            user_id=user_id,
        )
        if repository is None:
            raise HTTPException(status_code=404, detail="Repository not found")
        _validate_repository_mirror_target(repository=repository, clone_url=clone_url)

    try:
        row = await mirror_repo.upsert_target(
            user_id=user_id,
            source=source,
            clone_url=clone_url,
            name=body.name,
            repository_id=body.repository_id,
        )
    except Exception as exc:
        logger.exception(
            "git_mirror_register_failed",
            extra={"clone_url": clone_url, "correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to register mirror (correlation_id={correlation_id})",
        ) from exc

    return RegisterMirrorResponse(
        id=row.id,
        status=row.status.value if hasattr(row.status, "value") else str(row.status),
        clone_url=row.clone_url,
    )


@router.get("/search", response_model=GitMirrorSearchResponse)
async def search_mirrors(
    request: Request,
    q: str = Query(..., min_length=1, description="Semantic search query"),
    limit: int = Query(20, ge=1, le=100),
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(_get_db),
) -> GitMirrorSearchResponse:
    """Semantic search over non-GitHub git mirror READMEs indexed in Qdrant.

    Only mirrors with repository_id IS NULL (manual/arbitrary targets) are
    indexed and searchable via this endpoint. GitHub-linked mirrors are
    searchable via the repository search endpoint.
    """
    user_id: int = user["user_id"]
    correlation_id = getattr(request.state, "correlation_id", None)

    cfg = _get_app_config(request)

    try:
        from app.di.shared import build_qdrant_vector_store
        from app.infrastructure.embedding.embedding_factory import create_embedding_service
        from app.infrastructure.search.git_mirror_search_service import GitMirrorSearchService

        embedding_service = create_embedding_service(cfg.embedding)
        qdrant_store = await asyncio.to_thread(build_qdrant_vector_store, cfg)
        service = GitMirrorSearchService(
            embedding_service=embedding_service,
            qdrant_store=qdrant_store,
            db=db,
            environment=cfg.vector_store.environment,
            user_scope=cfg.vector_store.user_scope,
        )
        results = await service.search(
            q,
            user_id=user_id,
            limit=limit,
            correlation_id=correlation_id,
        )
    except Exception:
        logger.exception(
            "git_mirror_search_failed",
            extra={"user_id": user_id, "correlation_id": correlation_id},
        )
        return GitMirrorSearchResponse(items=[], total=0, limit=limit)

    items = [
        GitMirrorSearchItem(
            mirror_id=r.mirror_id,
            clone_url=r.clone_url,
            name=r.name,
            status=r.status,
            source=r.source,
            last_mirrored_at=r.last_mirrored_at,
            size_kb=r.size_kb,
            repository_id=r.repository_id,
            distance=r.distance,
        )
        for r in results.items
    ]
    return GitMirrorSearchResponse(items=items, total=results.total, limit=results.limit)


@router.get("/{mirror_id}", response_model=GitMirrorDetail)
async def get_mirror(
    mirror_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(_get_db),
) -> GitMirrorDetail:
    """Get full detail for a single git mirror."""
    user_id: int = user["user_id"]

    row = await _load_owned_mirror(db, mirror_id=mirror_id, user_id=user_id)

    if row is None:
        raise HTTPException(status_code=404, detail="Git mirror not found")

    return _mirror_to_detail(row)


@router.delete("/{mirror_id}", status_code=204)
async def delete_mirror(
    request: Request,
    mirror_id: int,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(_get_db),
) -> None:
    """Remove the git mirror DB row, its Qdrant vector point, and the on-disk bare clone (all best-effort).

    Order of operations:
    1. Load the row (capture mirror_path before deletion).
    2. Delete the DB row inside a transaction.
    3. Delete the Qdrant point best-effort (after DB row gone so a Qdrant error
       does not leave an orphaned DB row).
    4. Remove the on-disk bare clone directory best-effort (blocking rmtree
       offloaded to a thread).

    On-disk removal safety: the directory is only removed when mirror_path is
    non-empty AND it resolves to a path strictly inside GIT_BACKUP_DATA_PATH.
    This prevents path-traversal scenarios where a crafted mirror_path could
    reach outside the backup volume.  Any I/O error is swallowed so the DB
    deletion always succeeds.
    """
    user_id: int = user["user_id"]

    row = await _load_owned_mirror(db, mirror_id=mirror_id, user_id=user_id)

    if row is None:
        raise HTTPException(status_code=404, detail="Git mirror not found")

    # Capture mirror_path before we delete the row.
    mirror_path: str = row.mirror_path or ""

    async with db.transaction() as session:
        await session.execute(sql_delete(GitMirror).where(GitMirror.id == mirror_id))

    # Remove the Qdrant vector point best-effort (after DB row is deleted so a
    # failed Qdrant call does not leave an orphaned DB row).
    try:
        from qdrant_client.models import PointIdsList

        from app.di.shared import build_qdrant_vector_store
        from app.infrastructure.vector.point_ids import git_mirror_point_id, str_to_uuid

        cfg = _get_app_config(request)
        qdrant_store = await asyncio.to_thread(build_qdrant_vector_store, cfg)
        if qdrant_store.available:
            point_id = git_mirror_point_id(
                cfg.vector_store.environment,
                cfg.vector_store.user_scope,
                mirror_id,
            )
            await asyncio.to_thread(
                qdrant_store._client.delete,
                qdrant_store._collection_name,
                PointIdsList(points=[str_to_uuid(point_id)]),
                True,
            )
    except Exception:
        logger.warning(
            "git_mirror_delete_qdrant_point_failed",
            extra={"mirror_id": mirror_id, "user_id": user_id},
        )

    # Remove the on-disk bare clone directory best-effort.
    # Safety: only proceed when mirror_path resolves to a path that is strictly
    # inside cfg.git_backup.data_path to prevent path-traversal attacks.
    if mirror_path:
        try:
            import shutil
            from pathlib import Path

            cfg = _get_app_config(request)
            data_root = Path(cfg.git_backup.data_path).resolve()
            target = Path(mirror_path).resolve()
            if target != data_root and target.is_relative_to(data_root):

                def _rmtree() -> None:
                    if target.exists():
                        shutil.rmtree(target)

                await asyncio.to_thread(_rmtree)
                logger.info(
                    "git_mirror_delete_disk_removed",
                    extra={"mirror_id": mirror_id, "path": str(target)},
                )
            else:
                logger.warning(
                    "git_mirror_delete_disk_skipped_unsafe_path",
                    extra={
                        "mirror_id": mirror_id,
                        "mirror_path": mirror_path,
                        "data_root": str(data_root),
                    },
                )
        except Exception:
            logger.warning(
                "git_mirror_delete_disk_failed",
                extra={"mirror_id": mirror_id, "mirror_path": mirror_path},
            )
