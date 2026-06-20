"""Custom digest endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.adapters.email.service import EmailDeliveryError, EmailDeliveryService
from app.api.email_errors import raise_email_api_error
from app.api.models.digest import SendEmailRequest  # noqa: TC001
from app.api.models.requests import CreateCustomDigestRequest  # noqa: TC001
from app.api.models.responses import success_response
from app.api.routers.auth import get_current_user
from app.api.services.custom_digest_service import CustomDigestService
from app.config import load_config
from app.core.logging_utils import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.post("")
async def create_custom_digest(
    body: CreateCustomDigestRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a custom digest from a list of summary IDs."""
    payload = await CustomDigestService().create_digest(user_id=user["user_id"], body=body)
    return success_response(payload)


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
