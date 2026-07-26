"""Custom digest endpoints."""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.adapters.email.service import EmailDeliveryError, EmailDeliveryService
from app.api.email_errors import raise_email_api_error
from app.api.models.digest import SendEmailRequest  # noqa: TC001
from app.api.models.requests import CreateCustomDigestRequest  # noqa: TC001
from app.api.models.responses import success_response
from app.api.routers.auth import get_current_user
from app.api.services.custom_digest_service import CustomDigestService
from app.config import load_config
from app.core.logging_utils import generate_correlation_id, get_logger
from app.di.api import resolve_api_runtime
from app.di.repositories import build_llm_repository

logger = get_logger(__name__)
router = APIRouter()


def _get_custom_digest_service(request: Request) -> CustomDigestService:
    """Resolve a digest service with the runtime LLM dependencies when available."""
    with contextlib.suppress(RuntimeError):
        runtime = resolve_api_runtime(request)
        return CustomDigestService(
            session_manager=runtime.db,
            llm_client=runtime.core.llm_client,
            llm_repo=build_llm_repository(runtime.db),
        )
    return CustomDigestService()


@router.post("")
async def create_custom_digest(
    body: CreateCustomDigestRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    service: CustomDigestService = Depends(_get_custom_digest_service),
) -> dict[str, Any]:
    """Create a custom digest from a list of summary IDs."""
    correlation_id = getattr(request.state, "correlation_id", None) or generate_correlation_id()
    payload = await service.create_digest(
        user_id=user["user_id"],
        body=body,
        correlation_id=correlation_id,
    )
    return success_response(payload, correlation_id=correlation_id)


@router.get("")
async def list_custom_digests(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """List all custom digests for the current user, newest first."""
    digests = await CustomDigestService().list_digests(user_id=user["user_id"])
    return success_response({"digests": digests})


@router.get("/{digest_id}")
async def get_custom_digest(
    digest_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Get a specific custom digest by ID."""
    digest = await CustomDigestService().get_digest(user_id=user["user_id"], digest_id=digest_id)
    return success_response(digest)


@router.post("/{digest_id}/email")
async def email_custom_digest(
    digest_id: str,
    body: SendEmailRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Send a custom digest to a verified email address."""
    digest = await CustomDigestService().get_digest(user_id=user["user_id"], digest_id=digest_id)
    subject = str(digest.get("title") or "Ratatoskr custom digest")
    content = str(digest.get("content") or "")
    try:
        payload = await EmailDeliveryService(load_config().email).send_custom_content(
            user_id=user["user_id"],
            address_id=body.email_address_id,
            subject=subject,
            content=content,
            purpose="custom_digest",
            metadata={"custom_digest_id": digest_id},
        )
    except EmailDeliveryError as exc:
        raise_email_api_error(exc)
    return success_response(payload)
