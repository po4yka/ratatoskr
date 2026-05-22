"""Quick-Save endpoint for browser extension."""

from __future__ import annotations

import contextlib
from typing import Any, cast

from fastapi import APIRouter, Depends, Request

from app.api.exceptions import ValidationError
from app.api.models.requests import (  # noqa: TC001  # used at runtime in route body annotation
    QuickSaveRequest,
)
from app.api.models.responses import success_response
from app.api.routers.auth import get_current_user
from app.application.services.request_service import RequestService
from app.core.logging_utils import get_logger
from app.core.url_utils import normalize_url
from app.domain.exceptions.domain_exceptions import DuplicateResourceError
from app.domain.services.tag_service import normalize_tag_name, validate_tag_name

logger = get_logger(__name__)
router = APIRouter()


def _get_request_service(request: Request) -> RequestService:
    """Resolve the shared request workflow service from API runtime."""
    from app.di.api import resolve_api_runtime

    with contextlib.suppress(RuntimeError):
        return cast("RequestService", resolve_api_runtime(request).request_service)
    # Fallback for tests
    from app.api.dependencies.database import (
        get_crawl_result_repository,
        get_llm_repository,
        get_request_repository,
        get_session_manager,
        get_summary_repository,
    )

    db = get_session_manager()
    return RequestService(
        db=db,
        request_repository=get_request_repository(),
        summary_repository=get_summary_repository(),
        crawl_result_repository=get_crawl_result_repository(),
        llm_repository=get_llm_repository(),
    )


def _get_tag_repo() -> Any:
    """Lazily obtain the tag repository from the current API runtime."""
    from app.di.api import get_current_api_runtime

    runtime = get_current_api_runtime()
    return runtime.tag_repo


@router.post("/quick-save")
async def quick_save(
    request: Request,
    body: QuickSaveRequest,
    user: dict[str, Any] = Depends(get_current_user),
    request_service: RequestService = Depends(_get_request_service),
) -> Any:
    """Save a page from the browser extension.

    Normalizes the URL, checks for duplicates, optionally triggers
    summarization, and attaches tags.
    """
    input_url = str(body.url)

    # Normalize and deduplicate
    try:
        normalized_url = normalize_url(input_url)
    except ValueError as e:
        raise ValidationError(f"Invalid URL: {e}") from e
    # Check for existing request with same URL
    duplicate_info = await request_service.check_duplicate_url(user["user_id"], input_url)
    if duplicate_info:
        return success_response(
            {
                "request_id": duplicate_info.existing_request_id,
                "status": "duplicate",
                "title": body.title,
                "url": normalized_url,
                "duplicate": True,
                "summary_id": duplicate_info.existing_summary_id,
            }
        )

    try:
        new_request = await request_service.create_url_request(
            user_id=user["user_id"],
            input_url=input_url,
        )
    except DuplicateResourceError:
        dup = await request_service.check_duplicate_url(user["user_id"], input_url)
        return success_response(
            {
                "request_id": dup.existing_request_id if dup else None,
                "status": "duplicate",
                "title": body.title,
                "url": normalized_url,
                "duplicate": True,
                "summary_id": dup.existing_summary_id if dup else None,
            }
        )

    # If selected_text was provided, update the request's content_text
    if body.selected_text:
        await request_service.update_request_content_text(
            user_id=user["user_id"],
            request_id=new_request.id,
            content_text=body.selected_text,
        )

    # Find-or-create tags
    tags_attached: list[str] = []
    if body.tag_names:
        tag_repo = _get_tag_repo()
        for tag_name in body.tag_names:
            valid, err = validate_tag_name(tag_name)
            if not valid:
                raise ValidationError(err or f"Invalid tag name: {tag_name}")

            normalized_name = normalize_tag_name(tag_name)
            existing = await tag_repo.async_get_tag_by_normalized_name(
                user["user_id"], normalized_name
            )
            if existing is None:
                await tag_repo.async_create_tag(
                    user_id=user["user_id"],
                    name=tag_name.strip(),
                    normalized_name=normalized_name,
                    color=None,
                )
            tags_attached.append(tag_name.strip())

    # Schedule background summarization if requested
    if body.summarize:
        from app.di.api import resolve_api_runtime

        runtime = resolve_api_runtime(request)
        await runtime.durable_request_queue.enqueue(
            request_id=new_request.id,
            correlation_id=new_request.correlation_id,
        )

    return success_response(
        {
            "request_id": new_request.id,
            "status": "pending",
            "title": body.title,
            "url": normalized_url,
            "tags_attached": tags_attached,
            "duplicate": False,
        }
    )
