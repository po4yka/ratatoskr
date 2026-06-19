"""
Secret-key auth and management endpoints.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import datetime
from typing import Any, cast

from starlette.responses import Response  # noqa: TC002 - needed at runtime for FastAPI DI

from app.api.dependencies.database import get_auth_repository, get_user_repository
from app.api.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ResourceNotFoundError,
)
from app.api.models.auth import (
    SecretKeyActionResponse,
    SecretKeyCreateRequest,
    SecretKeyCreateResponse,
    SecretKeyListResponse,
    SecretKeyRevokeRequest,
    SecretKeyRotateRequest,
    SecretLoginRequest,
)
from app.api.models.responses import AuthTokensResponse, TokenPair, success_response
from app.api.routers.auth._fastapi import APIRouter, Depends
from app.api.routers.auth.cookies import set_refresh_cookie
from app.api.routers.auth.dependencies import get_current_user
from app.api.routers.auth.secret_auth import (
    build_secret_record,
    check_expired,
    coerce_naive,
    ensure_secret_login_enabled,
    ensure_user_allowed,
    generate_secret_value,
    handle_failed_attempt,
    hash_secret,
    reset_failed_attempts,
    serialize_secret,
    utcnow_naive,
    validate_secret_value,
)
from app.api.routers.auth.tokens import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    create_refresh_token,
    is_self_service_secret_client,
    is_web_client,
    validate_client_id,
)
from app.api.services.auth_service import AuthService
from app.core.logging_utils import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _extract_record_user_id(record: dict[str, Any]) -> int | None:
    record_user_id = record.get("user_id")
    if record_user_id is not None:
        return cast("int | None", record_user_id)

    user_field = record.get("user")
    if isinstance(user_field, dict):
        return cast("int | None", user_field.get("telegram_user_id"))
    if isinstance(user_field, int):
        return user_field
    return None


def _parse_naive_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


@router.post("/secret-login")
async def secret_login(login_data: SecretLoginRequest, response: Response) -> Any:
    """Exchange a pre-registered client secret for JWT tokens."""
    ensure_secret_login_enabled()
    validate_client_id(login_data.client_id)
    ensure_user_allowed(login_data.user_id)
    now = utcnow_naive()

    user_repo = get_user_repository()
    user = await user_repo.async_get_user_by_telegram_id(login_data.user_id)
    if not user:
        raise ResourceNotFoundError("User", login_data.user_id)

    auth_repo = get_auth_repository()
    secret_record = await auth_repo.async_get_client_secret(
        login_data.user_id, login_data.client_id
    )
    if not secret_record:
        raise AuthenticationError("Invalid credentials")

    if secret_record.get("status") == "revoked":
        logger.warning(
            "secret_login_revoked_secret",
            extra={"user_id": login_data.user_id, "client_id": login_data.client_id},
        )
        raise AuthenticationError("Secret has been revoked")

    if secret_record.get("status") == "locked":
        locked_until = secret_record.get("locked_until")
        locked_until_dt = _parse_naive_dt(locked_until)
        if locked_until_dt is None:
            logger.warning(
                "secret_login_locked_secret",
                extra={"user_id": login_data.user_id, "client_id": login_data.client_id},
            )
            raise AuthorizationError("Secret is temporarily locked")
        if locked_until_dt < now:
            await auth_repo.async_update_client_secret(
                secret_record["id"], owner_user_id=login_data.user_id, status="active"
            )
            await reset_failed_attempts(secret_record)
        else:
            logger.warning(
                "secret_login_locked_secret",
                extra={
                    "user_id": login_data.user_id,
                    "client_id": login_data.client_id,
                    "locked_until": locked_until_dt.isoformat() + "Z",
                },
            )
            raise AuthorizationError("Secret is temporarily locked")

    await check_expired(secret_record)

    provided_secret = validate_secret_value(login_data.secret, context="login")
    expected_hash = hash_secret(provided_secret, secret_record.get("secret_salt", ""))

    if not hmac.compare_digest(expected_hash, secret_record.get("secret_hash", "")):
        updated_record = await handle_failed_attempt(secret_record)
        logger.warning(
            "secret_login_failed",
            extra={
                "user_id": login_data.user_id,
                "client_id": login_data.client_id,
                "failed_attempts": updated_record.get("failed_attempts"),
                "status": updated_record.get("status"),
                "locked_until": updated_record.get("locked_until"),
            },
        )
        raise AuthenticationError("Invalid credentials")

    await reset_failed_attempts(secret_record)
    await auth_repo.async_update_client_secret(
        secret_record["id"],
        owner_user_id=login_data.user_id,
        last_used_at=now,
        status="active",
    )

    user_id = user.get("telegram_user_id", login_data.user_id)
    username = user.get("username", login_data.username)

    access_token = create_access_token(user_id, username, login_data.client_id)
    refresh_token, session_id = await create_refresh_token(user_id, login_data.client_id)

    web = is_web_client(login_data.client_id)
    if web:
        set_refresh_cookie(response, refresh_token)

    tokens = TokenPair(
        access_token=access_token,
        refresh_token=None if web else refresh_token,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        token_type="Bearer",
    )

    logger.info(
        "secret_login_success",
        extra={"user_id": user_id, "client_id": login_data.client_id, "session_id": session_id},
    )

    return success_response(AuthTokensResponse(tokens=tokens, session_id=session_id))


@router.post("/secret-keys")
async def create_secret_key(
    payload: SecretKeyCreateRequest, user: dict[str, Any] = Depends(get_current_user)
) -> Any:
    """Create or register a client secret for a user."""
    ensure_secret_login_enabled()
    validate_client_id(payload.client_id)
    acting_user = await AuthService.require_secret_key_manager(
        user,  # type: ignore[arg-type]
        target_user_id=payload.user_id,
        client_id=payload.client_id,
    )
    if acting_user.get("is_owner"):
        ensure_user_allowed(payload.user_id)
        target_user = await AuthService.get_or_create_target_user(payload.user_id, payload.username)
    else:
        target_user = acting_user
    target_user_id = target_user.get("telegram_user_id", payload.user_id)

    secret_value, record = await build_secret_record(
        target_user_id,
        payload.client_id,
        provided_secret=payload.secret,
        label=payload.label,
        description=payload.description,
        expires_at=coerce_naive(payload.expires_at),
    )

    logger.info(
        "secret_key_created",
        extra={
            "created_by": acting_user.get("telegram_user_id"),
            "user_id": target_user_id,
            "client_id": payload.client_id,
            "label": payload.label,
        },
    )

    return success_response(
        SecretKeyCreateResponse(secret=secret_value, key=serialize_secret(record))
    )


@router.post("/secret-keys/{key_id}/rotate")
async def rotate_secret_key(
    key_id: int, payload: SecretKeyRotateRequest, user: dict[str, Any] = Depends(get_current_user)
) -> Any:
    """Rotate an existing client secret."""
    ensure_secret_login_enabled()

    auth_repo = get_auth_repository()
    record = await auth_repo.async_get_client_secret_by_id(key_id)
    if not record:
        raise ResourceNotFoundError("Secret key", key_id)

    record_user_id = _extract_record_user_id(record)
    client_id = record.get("client_id", "")
    validate_client_id(client_id)
    acting_user = await AuthService.require_secret_key_manager(
        user,  # type: ignore[arg-type]
        target_user_id=record_user_id or 0,
        client_id=client_id,
    )
    if acting_user.get("is_owner"):
        ensure_user_allowed(record_user_id)

    if record.get("status") != "active":
        raise AuthenticationError("Only active secrets can be rotated")

    new_secret_value = (
        validate_secret_value(payload.secret, context="create")
        if payload.secret
        else generate_secret_value()
    )
    new_salt = secrets.token_hex(16)
    new_hash = hash_secret(new_secret_value, new_salt)

    await auth_repo.async_update_client_secret(
        key_id,
        owner_user_id=record_user_id,
        secret_salt=new_salt,
        secret_hash=new_hash,
        status="active",
        failed_attempts=0,
        locked_until=None,
        expires_at=coerce_naive(payload.expires_at) or record.get("expires_at"),
        label=payload.label if payload.label is not None else record.get("label"),
        description=payload.description
        if payload.description is not None
        else record.get("description"),
        last_used_at=None,
    )

    updated_record = await auth_repo.async_get_client_secret_by_id(key_id)

    logger.info(
        "secret_key_rotated",
        extra={
            "rotated_by": acting_user.get("telegram_user_id"),
            "user_id": record_user_id,
            "client_id": record.get("client_id"),
            "key_id": key_id,
        },
    )

    return success_response(
        SecretKeyCreateResponse(secret=new_secret_value, key=serialize_secret(updated_record or {}))
    )


@router.post("/secret-keys/{key_id}/revoke")
async def revoke_secret_key(
    key_id: int,
    payload: SecretKeyRevokeRequest | None = None,
    user: dict[str, Any] = Depends(get_current_user),
) -> Any:
    """Revoke an existing client secret."""
    ensure_secret_login_enabled()

    auth_repo = get_auth_repository()
    record = await auth_repo.async_get_client_secret_by_id(key_id)
    if not record:
        raise ResourceNotFoundError("Secret key", key_id)

    record_user_id = _extract_record_user_id(record)
    client_id = record.get("client_id", "")
    validate_client_id(client_id)
    acting_user = await AuthService.require_secret_key_manager(
        user,  # type: ignore[arg-type]
        target_user_id=record_user_id or 0,
        client_id=client_id,
    )
    if acting_user.get("is_owner"):
        ensure_user_allowed(record_user_id)

    if record.get("status") != "revoked":
        await auth_repo.async_update_client_secret(
            key_id,
            owner_user_id=record_user_id,
            status="revoked",
            failed_attempts=0,
            locked_until=None,
        )

    updated_record = await auth_repo.async_get_client_secret_by_id(key_id)

    logger.info(
        "secret_key_revoked",
        extra={
            "revoked_by": acting_user.get("telegram_user_id"),
            "user_id": record_user_id,
            "client_id": record.get("client_id"),
            "key_id": key_id,
            "reason": payload.reason if payload else None,
        },
    )

    return success_response(SecretKeyActionResponse(key=serialize_secret(updated_record or {})))


@router.get("/secret-keys")
async def list_secret_keys(
    user: dict[str, Any] = Depends(get_current_user),
    user_id: int | None = None,
    client_id: str | None = None,
    status: str | None = None,
) -> Any:
    """List stored client secrets visible to the current user."""
    ensure_secret_login_enabled()
    current_user = await AuthService.ensure_user(user["user_id"])
    is_owner = bool(current_user.get("is_owner"))
    if client_id is not None:
        validate_client_id(client_id)

    if is_owner:
        if user_id is not None:
            ensure_user_allowed(user_id)
    else:
        if user_id is not None and int(user_id) != int(current_user["telegram_user_id"]):
            raise AuthorizationError("You can only list your own client secrets")
        if client_id is not None:
            await AuthService.require_secret_key_manager(
                user,  # type: ignore[arg-type]
                target_user_id=current_user["telegram_user_id"],
                client_id=client_id,
            )
        user_id = int(current_user["telegram_user_id"])

    auth_repo = get_auth_repository()
    records = await auth_repo.async_list_client_secrets(
        user_id=user_id,
        client_id=client_id,
        status=status,
    )
    if not is_owner:
        records = [
            record
            for record in records
            if _extract_record_user_id(record) == int(current_user["telegram_user_id"])
            and is_self_service_secret_client(record.get("client_id"))
        ]

    keys = [serialize_secret(rec) for rec in records]
    return success_response(SecretKeyListResponse(keys=keys))
