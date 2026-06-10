"""
Telegram login and Telegram-linking endpoints.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timedelta
from typing import Any

from starlette.responses import Response  # noqa: TC002 - needed at runtime for FastAPI DI

from app.api.dependencies.database import get_user_repository
from app.api.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ProcessingError,
    ValidationError,
)
from app.api.models.auth import (
    TelegramLinkBeginResponse,
    TelegramLinkCompleteRequest,
    TelegramLoginRequest,
)
from app.api.models.responses import AuthTokensResponse, TokenPair, success_response
from app.api.routers.auth._fastapi import APIRouter, Depends
from app.api.routers.auth.cookies import set_refresh_cookie
from app.api.routers.auth.dependencies import get_current_user
from app.api.routers.auth.secret_auth import utcnow_naive
from app.api.routers.auth.telegram import verify_telegram_auth
from app.api.routers.auth.tokens import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    create_refresh_token,
    is_web_client,
    validate_client_id,
)
from app.api.services.auth_service import AuthService
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC

logger = get_logger(__name__)
router = APIRouter()


# Behavior verified by test_telegram_login_token_delivery in tests/api/test_auth_token_delivery.py
@router.post("/telegram-login")
async def telegram_login(login_data: TelegramLoginRequest, response: Response) -> Any:
    """
    Exchange Telegram authentication data for JWT tokens.

    Verifies Telegram auth hash using HMAC-SHA256 and returns access + refresh tokens.
    """
    try:
        validate_client_id(login_data.client_id)

        verify_telegram_auth(
            user_id=login_data.telegram_user_id,
            auth_hash=login_data.auth_hash,
            auth_date=login_data.auth_date,
            username=login_data.username,
            first_name=login_data.first_name,
            last_name=login_data.last_name,
            photo_url=login_data.photo_url,
        )

        user_repo = get_user_repository()
        user, created = await user_repo.async_get_or_create_user(
            login_data.telegram_user_id,
            username=login_data.username,
            is_owner=False,
        )

        user_id = user.get("telegram_user_id", login_data.telegram_user_id)
        username = user.get("username", login_data.username)

        access_token = create_access_token(user_id, username, login_data.client_id)
        refresh_token, session_id = await create_refresh_token(user_id, login_data.client_id)

        logger.info(
            "telegram_login_success",
            extra={
                "user_id": user_id,
                "username": username,
                "client_id": login_data.client_id,
                "created": created,
            },
        )

        web = is_web_client(login_data.client_id)
        if web:
            set_refresh_cookie(response, refresh_token)

        tokens = TokenPair(
            access_token=access_token,
            refresh_token=None if web else refresh_token,
            expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            token_type="Bearer",
        )
        return success_response(AuthTokensResponse(tokens=tokens, session_id=session_id))

    except (AuthenticationError, AuthorizationError, ConfigurationError, ValidationError):
        raise
    except Exception as e:
        logger.error(
            "telegram_login_failed",
            extra={"telegram_user_id": login_data.telegram_user_id},
            exc_info=True,
        )
        raise ProcessingError("Authentication failed. Please try again.") from e


@router.get("/me/telegram")
async def get_telegram_link_status(user: dict[str, Any] = Depends(get_current_user)) -> Any:
    """Fetch current Telegram link status."""
    user_record = await AuthService.ensure_user(user["user_id"])
    return success_response(AuthService.build_link_status_payload(user_record))


@router.post("/me/telegram/link")
async def begin_telegram_link(user: dict[str, Any] = Depends(get_current_user)) -> Any:
    """Begin linking by issuing a nonce."""
    await AuthService.ensure_user(user["user_id"])
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    nonce = secrets.token_urlsafe(32)
    await AuthService.set_link_nonce(user["user_id"], nonce, expires_at)
    return success_response(
        TelegramLinkBeginResponse(
            nonce=nonce, expires_at=AuthService.format_datetime(expires_at) or ""
        )
    )


@router.post("/me/telegram/complete")
async def complete_telegram_link(
    payload: TelegramLinkCompleteRequest, user: dict[str, Any] = Depends(get_current_user)
) -> Any:
    """Complete Telegram linking by validating nonce and Telegram login payload."""
    user_id = user["user_id"]
    user_record = await AuthService.ensure_user(user_id)

    link_nonce = user_record.get("link_nonce")
    link_nonce_expires_at = user_record.get("link_nonce_expires_at")

    if not link_nonce or not link_nonce_expires_at:
        raise ValidationError("Linking not initiated", details={"field": "nonce"})

    now = utcnow_naive()
    if not hmac.compare_digest(payload.nonce, link_nonce):
        raise ValidationError("Invalid link nonce", details={"field": "nonce"})

    expires_naive = link_nonce_expires_at
    if isinstance(expires_naive, str):
        expires_naive = datetime.fromisoformat(expires_naive.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    elif hasattr(expires_naive, "tzinfo"):
        expires_naive = expires_naive.replace(tzinfo=None)

    if expires_naive < now:
        raise ValidationError("Link nonce expired", details={"field": "nonce"})

    verify_telegram_auth(
        user_id=payload.telegram_user_id,
        auth_hash=payload.auth_hash,
        auth_date=payload.auth_date,
        username=payload.username,
        first_name=payload.first_name,
        last_name=payload.last_name,
        photo_url=payload.photo_url,
    )

    await AuthService.complete_telegram_link(
        user_id,
        payload.telegram_user_id,
        payload.username,
        payload.photo_url,
        payload.first_name,
        payload.last_name,
    )

    updated_user = await AuthService.ensure_user(user_id)

    logger.info(
        "telegram_linked",
        extra={
            "user_id": user_id,
            "linked_telegram_user_id": payload.telegram_user_id,
            "username": payload.username,
        },
    )

    return success_response(AuthService.build_link_status_payload(updated_user))


@router.delete("/me/telegram")
async def unlink_telegram(user: dict[str, Any] = Depends(get_current_user)) -> Any:
    """Unlink Telegram account."""
    user_id = user["user_id"]
    await AuthService.unlink_telegram(user_id)
    updated_user = await AuthService.ensure_user(user_id)
    logger.info("telegram_unlinked", extra={"user_id": user_id})
    return success_response(AuthService.build_link_status_payload(updated_user))
