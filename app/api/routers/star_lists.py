"""GitHub star-list management endpoints.

Star lists live only in GitHub's GraphQL schema, and every write here needs the
``user`` OAuth scope. A token connected with the default Ratatoskr scopes can
read lists but not modify them; GitHub answers such a write with a message
naming the missing scope, which is surfaced verbatim as a 403.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.models.responses.common import TypedSuccessResponse, success_response
from app.api.routers.auth import get_current_user
from app.application.exceptions.github import (
    GitHubAuthError,
    GitHubIntegrationRequiredError,
    GitHubRateLimitError,
)
from app.application.use_cases.manage_star_lists import (
    ManageStarListsUseCase,
    RepositoryNotOwnedError,
    StarListNotFoundError,
)
from app.core.logging_utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/star-lists", tags=["star-lists"])


class StarListResponse(BaseModel):
    """One star list."""

    id: str
    name: str
    slug: str
    description: str = ""
    is_private: bool = False


class StarListsResponse(BaseModel):
    """All star lists of the authenticated user."""

    lists: list[StarListResponse]


class CreateStarListRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    is_private: bool = False


class UpdateStarListRequest(BaseModel):
    """Every field is optional; omitted fields are left untouched."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    is_private: bool | None = None


class SetRepositoryListsRequest(BaseModel):
    """The complete set of lists the repository should belong to.

    This replaces the membership rather than adding to it — an empty array
    removes the repository from every list.
    """

    list_names: list[str] = Field(default_factory=list, max_length=32)


def _get_use_case(request: Request) -> ManageStarListsUseCase:
    """Resolve the pre-wired use case; routers stay transport-only."""
    from app.di.api import resolve_api_runtime

    use_case = resolve_api_runtime(request).star_lists_use_case
    if use_case is None:
        raise HTTPException(status_code=503, detail="Star list management is not available")
    return cast("ManageStarListsUseCase", use_case)


def _translate(exc: Exception) -> HTTPException:
    """Map a domain error onto the HTTP status the client should see."""
    if isinstance(exc, GitHubIntegrationRequiredError):
        return HTTPException(status_code=409, detail="GitHub integration required")
    if isinstance(exc, GitHubAuthError):
        # Carries GitHub's own wording, which names the scope the token lacks.
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, GitHubRateLimitError):
        return HTTPException(status_code=429, detail="GitHub rate limit exceeded")
    if isinstance(exc, StarListNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RepositoryNotOwnedError):
        return HTTPException(status_code=404, detail="Repository not found")
    logger.error("star_list_operation_failed: %s", exc, exc_info=True)
    return HTTPException(status_code=502, detail="GitHub star list operation failed")


@router.get("", response_model=TypedSuccessResponse[StarListsResponse])
async def list_star_lists(
    user: dict[str, Any] = Depends(get_current_user),
    use_case: ManageStarListsUseCase = Depends(_get_use_case),
) -> dict[str, Any]:
    """List the caller's star lists (metadata only — no repository items)."""
    try:
        entries = await use_case.list_lists(user["user_id"])
    except Exception as exc:
        raise _translate(exc) from exc
    return success_response(
        StarListsResponse(lists=[StarListResponse(**vars(entry)) for entry in entries])
    )


@router.post("", response_model=TypedSuccessResponse[StarListResponse], status_code=201)
async def create_star_list(
    body: CreateStarListRequest,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: ManageStarListsUseCase = Depends(_get_use_case),
) -> dict[str, Any]:
    """Create a star list. GitHub caps a user at 32 and rejects the 33rd itself."""
    try:
        created = await use_case.create_list(
            user["user_id"],
            name=body.name,
            description=body.description,
            is_private=body.is_private,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return success_response(StarListResponse(**vars(created)))


@router.patch("/{slug}", response_model=TypedSuccessResponse[StarListResponse])
async def update_star_list(
    slug: str,
    body: UpdateStarListRequest,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: ManageStarListsUseCase = Depends(_get_use_case),
) -> dict[str, Any]:
    """Rename or re-describe a list, addressed by slug or exact name."""
    try:
        updated = await use_case.update_list(
            user["user_id"],
            slug=slug,
            name=body.name,
            description=body.description,
            is_private=body.is_private,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return success_response(StarListResponse(**vars(updated)))


@router.delete("/{slug}", status_code=204)
async def delete_star_list(
    slug: str,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: ManageStarListsUseCase = Depends(_get_use_case),
) -> None:
    """Delete a list. The repositories in it keep their stars."""
    try:
        await use_case.delete_list(user["user_id"], slug=slug)
    except Exception as exc:
        raise _translate(exc) from exc


@router.put("/repositories/{repository_id}", response_model=TypedSuccessResponse[StarListsResponse])
async def set_repository_lists(
    repository_id: int,
    body: SetRepositoryListsRequest,
    user: dict[str, Any] = Depends(get_current_user),
    use_case: ManageStarListsUseCase = Depends(_get_use_case),
) -> dict[str, Any]:
    """Replace the lists a repository belongs to and refresh the local mirror.

    PUT semantics: the body is the complete desired membership, so an empty
    ``list_names`` removes the repository from every list.
    """
    try:
        applied = await use_case.set_repository_lists(
            repository_id=repository_id,
            user_id=user["user_id"],
            list_names=body.list_names,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return success_response(
        StarListsResponse(lists=[StarListResponse(id="", name=name, slug="") for name in applied])
    )
