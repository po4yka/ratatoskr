"""Magic-link email identity provider endpoints."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import Query
from starlette.responses import Response  # noqa: TC002 - FastAPI resolves this annotation

from app.adapters.email.service import EmailDeliveryService
from app.api.dependencies.database import get_session_manager
from app.api.exceptions import AuthenticationError
from app.api.models.auth import MagicLinkRequest  # noqa: TC001 - FastAPI resolves this model
from app.api.models.responses import success_response
from app.api.routers.auth._fastapi import APIRouter
from app.api.routers.auth.credential_auth import canonicalize_email, ensure_user_allowed
from app.api.routers.auth.identity_tokens import issue_auth_tokens
from app.api.routers.auth.tokens import validate_client_id
from app.config import load_config
from app.infrastructure.persistence.repositories.user_identity_repository import (
    UserIdentityRepository,
)

router = APIRouter()


@router.post("/magic-link/request")
async def request_magic_link(payload: MagicLinkRequest) -> Any:
    """Send a one-time login link to an existing user's email address."""
    validate_client_id(payload.client_id)
    display_email, email_canonical = canonicalize_email(payload.email)
    if not display_email or not email_canonical:
        raise AuthenticationError("Magic link could not be sent")

    cfg = load_config(allow_stub_telegram=True)
    repo = UserIdentityRepository(get_session_manager())
    user_id = await repo.async_find_user_id_by_email(email_canonical)
    if user_id is None:
        raise AuthenticationError("Magic link could not be sent")
    ensure_user_allowed(user_id)

    issued = await repo.async_issue_magic_link(
        user_id=user_id,
        email=display_email,
        email_canonical=email_canonical,
        client_id=payload.client_id,
        ttl=timedelta(minutes=15),
    )
    link = _magic_link_url(
        cfg.auth.magic_link_verify_url, token=issued.token, client_id=payload.client_id
    )
    email_result = await EmailDeliveryService(cfg.email).send_magic_link(
        user_id=user_id,
        recipient=display_email,
        link=link,
    )
    return success_response(
        {
            "status": "sent" if email_result.get("email_sent") else "pending",
            "emailSent": bool(email_result.get("email_sent")),
            "expiresAt": issued.expires_at.isoformat(),
            **({"magicLink": email_result["magic_link"]} if "magic_link" in email_result else {}),
        }
    )


@router.get("/magic-link/verify")
async def verify_magic_link(
    response: Response,
    token: str = Query(..., min_length=16, max_length=256),
) -> Any:
    """Consume a magic-link token and issue standard JWTs."""
    repo = UserIdentityRepository(get_session_manager())
    record = await repo.async_consume_magic_link(token)
    if record is None:
        raise AuthenticationError("Invalid or expired magic link")
    user_id = int(record["user_id"])
    ensure_user_allowed(user_id)
    email = str(record["email"])
    email_canonical = str(record["email_canonical"])
    await repo.async_upsert_identity(
        user_id=user_id,
        provider="magic_link",
        subject=email_canonical,
        email=email,
        email_canonical=email_canonical,
        email_verified=True,
    )
    redeemed_client_id = str(record["client_id"])
    validate_client_id(redeemed_client_id)
    return await issue_auth_tokens(
        user_id=user_id,
        username=None,
        client_id=redeemed_client_id,
        response=response,
    )


def _magic_link_url(base_url: str | None, *, token: str, client_id: str) -> str:
    params = urlencode({"token": token, "client_id": client_id})
    base = (base_url or "/v1/auth/magic-link/verify").strip()
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{params}"


__all__ = ["request_magic_link", "router", "verify_magic_link"]
