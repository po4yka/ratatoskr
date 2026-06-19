"""
Token refresh, logout, and session listing endpoints.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from starlette.requests import Request  # noqa: TC002 - needed at runtime for FastAPI DI
from starlette.responses import Response  # noqa: TC002 - needed at runtime for FastAPI DI

from app.api.dependencies.database import get_user_repository
from app.api.exceptions import AuthorizationError, ResourceNotFoundError
from app.api.models.auth import RefreshTokenRequest, SessionInfo
from app.api.models.responses import (
    AuthTokensResponse,
    SessionListResponse,
    TokenPair,
    success_response,
)
from app.api.routers.auth._fastapi import APIRouter, Depends
from app.api.routers.auth.cookies import (
    REFRESH_COOKIE_NAME,
    clear_refresh_cookie,
    set_refresh_cookie,
)
from app.api.routers.auth.dependencies import get_auth_repository, get_current_user
from app.api.routers.auth.tokens import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    create_refresh_token,
    decode_token,
    is_web_client,
    validate_client_id,
)
from app.core.logging_utils import get_logger, log_exception
from app.core.time_utils import UTC
from app.security.token_family_policy import (
    FamilyDecisionKind,
    FamilyTokenRecord,
    TokenFamilyPolicy,
)

logger = get_logger(__name__)
router = APIRouter()


def _get_audit_log_repository() -> Any:
    """Construct the audit-log repository against the runtime DB cache.

    Local helper rather than a FastAPI dep so it works for handlers that
    are also called directly from tests.
    """
    from app.api.dependencies.database import resolve_repository_session
    from app.infrastructure.persistence.repositories.audit_log_repository import (
        AuditLogRepositoryAdapter,
    )

    return AuditLogRepositoryAdapter(resolve_repository_session(None, None))


def _to_family_record(row: dict[str, Any]) -> FamilyTokenRecord:
    """Coerce an auth-repo dict row into the policy's FamilyTokenRecord."""
    return FamilyTokenRecord(
        token_hash=row["token_hash"],
        family_id=row["family_id"],
        is_revoked=bool(row.get("is_revoked", False)),
        expires_at=row["expires_at"],
        parent_token_hash=row.get("parent_token_hash"),
    )


def _record_token_family_decision_metric(kind: FamilyDecisionKind) -> None:
    try:
        from app.observability.metrics import record_token_family_decision

        record_token_family_decision(kind.value)
    except Exception as exc:
        log_exception(logger, "token_family_decision_metric_failed", exc, level="debug")


def _format_dt_z(dt_value: Any) -> str:
    if dt_value is None:
        return ""
    if hasattr(dt_value, "isoformat"):
        return str(dt_value.isoformat()) + "Z"
    value = str(dt_value)
    return value if value.endswith("Z") else value + "Z"


@router.post("/refresh")
async def refresh_access_token(
    request: Request,
    response: Response,
    refresh_data: RefreshTokenRequest,
    auth_repo: Any = Depends(get_auth_repository),
) -> Any:
    """Refresh an expired access token using a refresh token."""
    from app.api.exceptions import TokenInvalidError, TokenRevokedError

    # Resolve refresh token: prefer body, fall back to httpOnly cookie
    raw_token = refresh_data.refresh_token or request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_token:
        raise TokenInvalidError("No refresh token provided")

    payload = decode_token(raw_token, expected_type="refresh")
    user_id = payload.get("user_id")
    if not user_id:
        raise TokenInvalidError("Missing user_id in token payload")

    client_id = payload.get("client_id")
    validate_client_id(client_id)

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    refresh_token_record = await auth_repo.async_get_refresh_token_by_hash(token_hash)
    if not refresh_token_record:
        raise TokenInvalidError("Refresh token is not recognized")

    # Token-family rotation policy (see app/security/token_family_policy.py):
    # load the whole family and let the pure policy decide. The cached
    # `get_refresh_token_by_hash` may omit `family_id`, so always re-read
    # against the DB before consulting the policy.
    family_id = refresh_token_record.get("family_id")
    if family_id is None:
        # Legacy row pre-migration-0016 (defensive — migration 0016 backfills,
        # so this should not happen in practice). Reject without cascading.
        clear_refresh_cookie(response)
        raise TokenInvalidError("Refresh token is missing family metadata")

    family_rows = await auth_repo.async_get_family_records(family_id, owner_user_id=user_id)
    # The presented token must be one of the family rows (it must contain
    # `family_id` and matching `token_hash`). Find it for the policy call.
    presented = next(
        (_to_family_record(r) for r in family_rows if r.get("token_hash") == token_hash),
        None,
    )
    if presented is None:
        clear_refresh_cookie(response)
        raise TokenInvalidError("Refresh token is not in its declared family")

    now = datetime.now(UTC)
    decision = TokenFamilyPolicy.decide(
        presented_token=presented,
        family_records=[_to_family_record(r) for r in family_rows],
        now=now,
    )
    _record_token_family_decision_metric(decision.kind)

    if decision.kind is FamilyDecisionKind.REVOKE_FAMILY:
        revoked_hashes = await auth_repo.async_revoke_family(family_id, owner_user_id=user_id)
        try:
            audit_repo = _get_audit_log_repository()
            await audit_repo.async_insert_audit_log(
                "warning",
                "refresh_family_revoked",
                details={
                    "family_id": family_id,
                    "user_id": user_id,
                    "presented_token_hash_prefix": token_hash[:8],
                    "client_id": client_id,
                    "source_ip": request.client.host if request.client else None,
                    "reason": "retired_token_replay",
                    "revoked_count": len(revoked_hashes),
                },
            )
        except Exception as exc:
            log_exception(logger, "refresh_family_audit_write_failed", exc, level="warning")
        logger.warning(
            "refresh_token_reuse_detected",
            extra={
                "user_id": user_id,
                "family_id": family_id,
                "revoked_count": len(revoked_hashes),
            },
        )
        clear_refresh_cookie(response)
        raise TokenRevokedError()

    if decision.kind is FamilyDecisionKind.REJECT:
        clear_refresh_cookie(response)
        raise TokenInvalidError("Refresh token is no longer valid")

    user_repo = get_user_repository()
    user = await user_repo.async_get_user_by_telegram_id(user_id)
    if not user:
        raise ResourceNotFoundError("User", user_id)

    # Preserve the TTL family across rotation: a session that began as a
    # short-lived non-remembered credentials login must rotate into another
    # short-lived token (and session-cookie max_age=None), and vice versa.
    # Default to True for legacy rows persisted before remember_me existed.
    remember_me = bool(refresh_token_record.get("remember_me", True))

    if remember_me:
        ttl_seconds: float = 30 * 24 * 60 * 60
        cookie_max_age: int | None = 30 * 24 * 60 * 60
    else:
        # Match the credentials-login no-remember TTL (read from config to keep
        # this in sync with credentials_no_remember_hours).
        from app.config import load_config

        cfg = load_config(allow_stub_telegram=True)
        ttl_seconds = cfg.auth.credentials_no_remember_hours * 60 * 60
        cookie_max_age = None  # session cookie -- vanishes on browser close

    # Rotate: revoke old token, issue new one chained into the same family.
    await auth_repo.async_revoke_refresh_token(token_hash)
    new_refresh_token, session_id = await create_refresh_token(
        user_id=user_id,
        client_id=client_id,
        auth_repo=auth_repo,
        ttl_seconds=ttl_seconds,
        remember_me=remember_me,
        parent_family_id=family_id,
        parent_token_hash=token_hash,
    )

    access_token = create_access_token(
        user.get("telegram_user_id", user_id),
        user.get("username"),
        client_id,
    )

    logger.info(
        "session_refreshed",
        extra={
            "user_id": user_id,
            "client_id": client_id,
            "remember_me": remember_me,
        },
    )

    # Apply delivery policy: web clients get an httpOnly cookie and no body
    # token; non-web clients (mobile, CLI, MCP) get the token in the body.
    # max_age=None issues a session cookie (vanishes on browser close) for
    # the credentials-login no-remember mode.
    web = is_web_client(client_id)
    if web:
        set_refresh_cookie(response, new_refresh_token, max_age=cookie_max_age)

    tokens = TokenPair(
        access_token=access_token,
        refresh_token=None if web else new_refresh_token,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        token_type="Bearer",
    )
    return success_response(AuthTokensResponse(tokens=tokens, session_id=session_id))


@router.post("/logout")
async def logout(
    http_request: Request,
    response: Response,
    request: RefreshTokenRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
    auth_repo: Any = Depends(get_auth_repository),
) -> Any:
    """Logout by revoking the specific refresh token."""
    token = request.refresh_token or http_request.cookies.get(REFRESH_COOKIE_NAME)
    if token:
        try:
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            record = await auth_repo.async_get_refresh_token_by_hash(token_hash)
            if record is None:
                raise ResourceNotFoundError("RefreshToken", token_hash[:8])
            # SQLAlchemy model_to_dict emits "user_id"; legacy cache entries may carry "user".
            record_user_id = record.get("user_id") or record.get("user")
            if str(record_user_id) != str(current_user.get("user_id")):
                raise AuthorizationError("Token does not belong to the authenticated user")
            revoked = await auth_repo.async_revoke_refresh_token(token_hash)
            if revoked:
                logger.info("refresh_session_revoked")
        except (ResourceNotFoundError, AuthorizationError):
            raise
        except Exception as e:
            log_exception(logger, "logout_failed", e, level="warning")

    clear_refresh_cookie(response)
    return success_response({"message": "Logged out successfully"})


@router.get("/sessions")
async def list_sessions(
    current_user: dict[str, Any] = Depends(get_current_user),
    auth_repo: Any = Depends(get_auth_repository),
) -> dict[str, Any]:
    """List active sessions for the current user."""
    user_id = current_user["user_id"]
    now = datetime.now(UTC)

    sessions = await auth_repo.async_list_active_sessions(user_id, now)

    formatted_sessions = []
    for s in sessions:
        formatted_sessions.append(
            SessionInfo(
                id=s.get("id", 0),
                client_id=s.get("client_id"),
                device_info=s.get("device_info"),
                ip_address=s.get("ip_address"),
                last_used_at=_format_dt_z(s.get("last_used_at")),
                created_at=_format_dt_z(s.get("created_at")),
            )
        )

    return success_response(SessionListResponse(sessions=formatted_sessions))


@router.delete("/sessions/{session_id}")
async def revoke_session(
    session_id: int,
    current_user: dict[str, Any] = Depends(get_current_user),
    auth_repo: Any = Depends(get_auth_repository),
) -> dict[str, Any]:
    """Revoke a specific session by ID. Cannot revoke the current session via this endpoint."""
    user_id = current_user["user_id"]
    revoked = await auth_repo.async_revoke_session_by_id(session_id, user_id)
    if not revoked:
        raise ResourceNotFoundError("Session", session_id)
    logger.info("session_revoked", extra={"user_id": user_id, "session_id": session_id})
    return success_response({"id": session_id, "revoked": True})


@router.post("/logout-all")
async def logout_all(
    response: Response,
    current_user: dict[str, Any] = Depends(get_current_user),
    auth_repo: Any = Depends(get_auth_repository),
) -> Any:
    """Revoke every active refresh-token family for the current user.

    Enumerates the distinct ``family_id`` values across the user's active
    refresh tokens via :meth:`TokenFamilyPolicy.family_ids_for_user`, then
    bulk-revokes each family and writes one ``AuditLog`` row per family.
    The current refresh cookie is cleared so the browser session ends.
    """
    user_id = current_user["user_id"]
    active_rows = await auth_repo.async_list_active_family_records_for_user(user_id)
    family_records = [_to_family_record(r) for r in active_rows]
    family_ids = TokenFamilyPolicy.family_ids_for_user(family_records)

    audit_repo = _get_audit_log_repository()
    revoked_count = 0
    for fid in family_ids:
        hashes = await auth_repo.async_revoke_family(fid, owner_user_id=user_id)
        revoked_count += len(hashes)
        try:
            await audit_repo.async_insert_audit_log(
                "info",
                "refresh_family_revoked",
                details={
                    "family_id": fid,
                    "user_id": user_id,
                    "reason": "logout_all",
                    "revoked_count": len(hashes),
                },
            )
        except Exception as exc:
            log_exception(logger, "logout_all_audit_write_failed", exc, level="warning")

    logger.info(
        "logout_all_completed",
        extra={
            "user_id": user_id,
            "revoked_families": len(family_ids),
            "revoked_tokens": revoked_count,
        },
    )
    clear_refresh_cookie(response)
    return success_response(
        {
            "revokedFamilies": len(family_ids),
            "revokedTokens": revoked_count,
        }
    )
