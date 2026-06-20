"""Tag management endpoints."""

from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies.database import get_summary_repository
from app.api.exceptions import ResourceNotFoundError, ValidationError
from app.api.models.requests import (
    AttachTagsRequest,
    CreateTagRequest,
    MergeTagsRequest,
    UpdateTagRequest,
)
from app.api.models.responses import TagListResponse, TagResponse, success_response
from app.api.routers.auth import get_current_user
from app.api.search_helpers import isotime
from app.core.logging_utils import get_logger
from app.domain.services.tag_service import (
    normalize_tag_name,
    validate_tag_color,
    validate_tag_name,
)

logger = get_logger(__name__)

router = APIRouter()
summary_tags_router = APIRouter()


def _get_tag_repo() -> Any:
    """Lazily obtain the tag repository from the current API runtime."""
    from app.di.api import get_current_api_runtime

    runtime = get_current_api_runtime()
    return runtime.tag_repo


def _tag_to_response(tag: dict[str, Any], summary_count: int = 0) -> TagResponse:
    """Convert a tag dict to a TagResponse."""
    return TagResponse(
        id=tag["id"],
        name=tag["name"],
        color=tag.get("color"),
        summary_count=tag.get("summary_count", summary_count),
        created_at=isotime(tag.get("created_at")),
        updated_at=isotime(tag.get("updated_at")),
    )


def _verify_tag_ownership(tag: dict[str, Any] | None, tag_id: int, user_id: int) -> dict[str, Any]:
    """Verify that the tag exists and belongs to the user."""
    if tag is None or tag.get("is_deleted"):
        raise ResourceNotFoundError("Tag", tag_id)
    if tag.get("user") != user_id and tag.get("user_id") != user_id:
        raise ResourceNotFoundError("Tag", tag_id)
    return tag


def _dedupe_ints(values: list[int] | None) -> list[int]:
    seen: dict[int, None] = {}
    for value in values or []:
        seen.setdefault(value, None)
    return list(seen)


def _dedupe_tag_names(values: list[str] | None) -> list[str]:
    seen: dict[str, str] = {}
    for value in values or []:
        normalized = normalize_tag_name(value)
        seen.setdefault(normalized, value)
    return list(seen.values())


async def _ensure_summary_owned(summary_id: int, user_id: int) -> None:
    """Raise if the summary does not belong to the authenticated user."""
    summary = await get_summary_repository().async_get_summary_by_id(summary_id)
    if summary is None or summary.get("user_id") != user_id:
        raise ResourceNotFoundError("Summary", summary_id)


# --- Tag CRUD endpoints ---


@router.get("/")
async def list_tags(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """List all tags for the current user."""
    repo = _get_tag_repo()
    tags = await repo.async_get_user_tags(user["user_id"])
    items = [_tag_to_response(t) for t in tags]
    return success_response(TagListResponse(tags=items))


@router.post("/", status_code=201)
async def create_tag(
    body: CreateTagRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a new tag."""
    valid, err = validate_tag_name(body.name)
    if not valid:
        raise ValidationError(err or "Invalid tag name")

    if body.color is not None:
        valid, err = validate_tag_color(body.color)
        if not valid:
            raise ValidationError(err or "Invalid tag color")

    normalized = normalize_tag_name(body.name)
    repo = _get_tag_repo()

    existing = await repo.async_get_tag_by_normalized_name(user["user_id"], normalized)
    if existing is not None:
        raise ValidationError(f"Tag '{body.name}' already exists")

    tag = await repo.async_create_tag(
        user_id=user["user_id"],
        name=body.name.strip(),
        normalized_name=normalized,
        color=body.color,
    )
    return success_response(_tag_to_response(tag))


@router.get("/{tag_id}")
async def get_tag(
    tag_id: int,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Get tag details."""
    repo = _get_tag_repo()
    tag = await repo.async_get_tag_by_id(tag_id)
    tag = _verify_tag_ownership(tag, tag_id, user["user_id"])
    return success_response(_tag_to_response(tag))


@router.patch("/{tag_id}")
async def update_tag(
    tag_id: int,
    body: UpdateTagRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Update a tag's name or color."""
    repo = _get_tag_repo()
    tag = await repo.async_get_tag_by_id(tag_id)
    _verify_tag_ownership(tag, tag_id, user["user_id"])

    if body.name is not None:
        valid, err = validate_tag_name(body.name)
        if not valid:
            raise ValidationError(err or "Invalid tag name")
        normalized = normalize_tag_name(body.name)
        existing = await repo.async_get_tag_by_normalized_name(user["user_id"], normalized)
        if existing is not None and existing["id"] != tag_id:
            raise ValidationError(f"Tag '{body.name}' already exists")

    if body.color is not None:
        valid, err = validate_tag_color(body.color)
        if not valid:
            raise ValidationError(err or "Invalid tag color")

    updated = await repo.async_update_tag(
        tag_id, name=body.name, color=body.color, user_id=user["user_id"]
    )
    return success_response(_tag_to_response(updated))


@router.delete("/{tag_id}")
async def delete_tag(
    tag_id: int,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Soft-delete a tag."""
    repo = _get_tag_repo()
    tag = await repo.async_get_tag_by_id(tag_id)
    _verify_tag_ownership(tag, tag_id, user["user_id"])

    await repo.async_delete_tag(tag_id, user_id=user["user_id"])
    return success_response({"deleted": True, "id": tag_id})


@router.post("/merge")
async def merge_tags(
    body: MergeTagsRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Merge source tags into a target tag."""
    repo = _get_tag_repo()
    source_tag_ids = _dedupe_ints(body.source_tag_ids)

    # Verify ownership of target
    target = await repo.async_get_tag_by_id(body.target_tag_id)
    _verify_tag_ownership(target, body.target_tag_id, user["user_id"])

    # Verify ownership of all sources
    for src_id in source_tag_ids:
        src = await repo.async_get_tag_by_id(src_id)
        _verify_tag_ownership(src, src_id, user["user_id"])

    if body.target_tag_id in source_tag_ids:
        raise ValidationError("Target tag cannot be in source tags")

    await repo.async_merge_tags(source_tag_ids, body.target_tag_id, user_id=user["user_id"])
    return success_response({"merged": True, "target_tag_id": body.target_tag_id})


# --- Summary-tag attachment endpoints ---


@summary_tags_router.post("/{summary_id}/tags", status_code=201)
async def attach_tags(
    summary_id: int,
    body: AttachTagsRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Attach tags to a summary by ID or name (auto-create if needed)."""
    await _ensure_summary_owned(summary_id, user["user_id"])

    repo = _get_tag_repo()
    attached: list[dict[str, Any]] = []
    tag_ids = _dedupe_ints(body.tag_ids)
    tag_names = _dedupe_tag_names(body.tag_names)

    # Validate all explicit tag IDs before attaching any of them so a mixed owned/cross-user batch cannot partially mutate state.
    for tid in tag_ids:
        tag = await repo.async_get_tag_by_id(tid)
        _verify_tag_ownership(tag, tid, user["user_id"])

    for tid in tag_ids:
        assoc = await repo.async_attach_tag(summary_id, tid, source="manual")
        attached.append(assoc)

    # Attach by tag names (auto-create if needed)
    if tag_names:
        for name in tag_names:
            valid, err = validate_tag_name(name)
            if not valid:
                raise ValidationError(err or f"Invalid tag name: {name}")

            normalized = normalize_tag_name(name)
            existing = await repo.async_get_tag_by_normalized_name(user["user_id"], normalized)
            if existing is not None:
                tid = existing["id"]
            else:
                created = await repo.async_create_tag(
                    user_id=user["user_id"],
                    name=name.strip(),
                    normalized_name=normalized,
                    color=None,
                )
                tid = created["id"]
            assoc = await repo.async_attach_tag(summary_id, tid, source="manual")
            attached.append(assoc)

    if not tag_ids and not tag_names:
        raise ValidationError("At least one of tag_ids or tag_names must be provided")

    # Return current tags for the summary
    tags = await repo.async_get_tags_for_summary(summary_id)
    items = [_tag_to_response(t) for t in tags]
    return success_response(TagListResponse(tags=items))


@summary_tags_router.delete("/{summary_id}/tags/{tag_id}")
async def detach_tag(
    summary_id: int,
    tag_id: int,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Detach a tag from a summary."""
    await _ensure_summary_owned(summary_id, user["user_id"])

    repo = _get_tag_repo()
    tag = await repo.async_get_tag_by_id(tag_id)
    _verify_tag_ownership(tag, tag_id, user["user_id"])
    await repo.async_detach_tag(summary_id, tag_id)
    return success_response({"detached": True, "summary_id": summary_id, "tag_id": tag_id})
