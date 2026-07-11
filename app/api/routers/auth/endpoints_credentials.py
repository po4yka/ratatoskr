"""Nickname/email + password login endpoints.

Coexists with /telegram-login and /secret-login. Anti-enumeration: every
failure path returns the same uniform 401 ("Invalid credentials") with
matching wall-clock timing (decoy argon2 verify when the identifier doesn't
resolve or the user is not allowlisted).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from starlette.responses import Response  # noqa: TC002 - needed at runtime for FastAPI DI

from app.api.dependencies.database import (
    get_user_credential_repository,
    get_user_repository,
)
from app.api.exceptions import AuthenticationError, ResourceNotFoundError
from app.api.models.auth import (  # noqa: TC001 - FastAPI resolves these at runtime
    ChangePasswordRequest,
    CredentialsLoginRequest,
)
from app.api.models.responses import AuthTokensResponse, TokenPair, success_response
from app.api.routers.auth._fastapi import APIRouter, Depends
from app.api.routers.auth.cookies import set_refresh_cookie
from app.api.routers.auth.credential_auth import (
    canonicalize_identifier,
    ensure_user_allowed,
    hash_password,
    run_decoy_verify,
    validate_password,
    verify_password,
)
from app.api.routers.auth.dependencies import get_auth_repository, get_current_user
from app.api.routers.auth.tokens import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    create_refresh_token,
    is_web_client,
    validate_client_id,
)
from app.config import load_config
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.security.token_family_policy import FamilyTokenRecord, TokenFamilyPolicy

logger = get_logger(__name__)
router = APIRouter()


def _generic_failure() -> AuthenticationError:
    """Single chokepoint for the canonical 401 -- callers MUST use this."""
    return AuthenticationError("Invalid credentials")


def _to_family_record(row: dict[str, Any]) -> FamilyTokenRecord:
    """Coerce an auth-repo dict row into the policy's FamilyTokenRecord.

    Mirrors the identically-named helper in ``endpoints_sessions.py``.
    """
    return FamilyTokenRecord(
        token_hash=row["token_hash"],
        family_id=row["family_id"],
        is_revoked=bool(row.get("is_revoked", False)),
        expires_at=row["expires_at"],
        parent_token_hash=row.get("parent_token_hash"),
    )


def _coerce_naive(dt_value: datetime | None) -> datetime | None:
    if dt_value is None:
        return None
    if dt_value.tzinfo is None:
        return dt_value.replace(tzinfo=UTC)
    return dt_value


@router.post("/credentials-login")
async def credentials_login(
    payload: CredentialsLoginRequest,
    response: Response,
) -> Any:
    """Exchange nickname/email + password for JWT access + refresh tokens.

    Remember Me semantics:
      - True  -> 30-day refresh TTL, web client stores tokens in localStorage.
      - False -> 12-hour refresh TTL (configurable via
                 CREDENTIALS_LOGIN_NO_REMEMBER_HOURS), refresh cookie issued
                 as a session cookie (no Max-Age), web client stores tokens
                 in sessionStorage so they vanish on browser close.
    """
    validate_client_id(payload.client_id)
    validate_password(payload.password)
    cred_repo = get_user_credential_repository()

    kind, _display, canonical = canonicalize_identifier(payload.identifier)

    if kind == "email":
        record = await cred_repo.async_get_by_canonical(email_canonical=canonical)
    else:
        record = await cred_repo.async_get_by_canonical(nickname_canonical=canonical)

    # Decoy verify on the no-row-found path so timing matches the success path.
    if record is None:
        run_decoy_verify(payload.password)
        logger.warning("credentials_login_unknown_identifier", extra={"kind": kind})
        raise _generic_failure()

    user_id = record.get("user_id")
    if user_id is None:
        run_decoy_verify(payload.password)
        raise _generic_failure()

    # Allowlist check: a credential row that maps to a non-allowed user must
    # still cost a verify so an attacker can't enumerate via timing.
    try:
        ensure_user_allowed(int(user_id))
    except Exception:
        run_decoy_verify(payload.password)
        raise _generic_failure() from None

    # Lockout check: a still-locked row returns 401 + Retry-After. We do NOT
    # auto-clear lockouts on the read path -- only on a successful verify.
    now = datetime.now(UTC)
    locked_until = _coerce_naive(record.get("locked_until"))
    if locked_until is not None and locked_until > now:
        seconds = max(int((locked_until - now).total_seconds()), 1)
        logger.warning(
            "credentials_login_locked",
            extra={"user_id": user_id, "locked_until": locked_until.isoformat()},
        )
        raise AuthenticationError("Invalid credentials", retry_after=seconds)

    cfg = load_config(allow_stub_telegram=True)
    auth_cfg = cfg.auth
    matched, needs_rehash = verify_password(
        payload.password, record["password_hash"], record.get("pepper_version", 1)
    )

    if not matched:
        updated = await cred_repo.async_record_failure(
            record["id"],
            max_attempts=auth_cfg.credentials_max_failed_attempts,
            lockout_minutes=auth_cfg.credentials_lockout_minutes,
        )
        logger.warning(
            "credentials_login_failed",
            extra={
                "user_id": user_id,
                "failed_attempts": updated.get("failed_attempts"),
                "locked_until": updated.get("locked_until"),
            },
        )
        raise _generic_failure()

    # Success: clear failure counters, touch last_login_at, opportunistically
    # rehash if argon2 reports the stored hash is below current cost params.
    await cred_repo.async_reset_failure(record["id"])
    await cred_repo.async_touch_last_login(record["id"], now)
    if needs_rehash:
        new_phc, new_version = hash_password(payload.password)
        await cred_repo.async_update_password_hash(
            record["id"], password_hash=new_phc, pepper_version=new_version
        )

    # Resolve user info for token claims.
    user_repo = get_user_repository()
    user = await user_repo.async_get_user_by_telegram_id(int(user_id))
    if not user:
        raise ResourceNotFoundError("User", user_id)

    # Remember Me decides both refresh TTL and cookie persistence.
    if payload.remember_me:
        refresh_ttl_seconds: float = auth_cfg.credentials_remember_me_days * 24 * 60 * 60
        cookie_max_age: int | None = int(refresh_ttl_seconds)
    else:
        refresh_ttl_seconds = auth_cfg.credentials_no_remember_hours * 60 * 60
        cookie_max_age = None  # session cookie -- vanishes on browser close

    access_token = create_access_token(
        int(user_id),
        user.get("username"),
        payload.client_id,
    )
    refresh_token, session_id = await create_refresh_token(
        int(user_id),
        payload.client_id,
        ttl_seconds=refresh_ttl_seconds,
        remember_me=payload.remember_me,
    )

    web = is_web_client(payload.client_id)
    if web:
        set_refresh_cookie(response, refresh_token, max_age=cookie_max_age)

    tokens = TokenPair(
        access_token=access_token,
        refresh_token=None if web else refresh_token,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        token_type="Bearer",
    )

    logger.info(
        "credentials_login_success",
        extra={
            "user_id": user_id,
            "client_id": payload.client_id,
            "session_id": session_id,
            "remember_me": payload.remember_me,
            "kind": kind,
        },
    )

    return success_response(AuthTokensResponse(tokens=tokens, session_id=session_id))


@router.post("/credentials/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    user: dict[str, Any] = Depends(get_current_user),
    auth_repo: Any = Depends(get_auth_repository),
) -> Any:
    """Change the current user's password and revoke all active refresh-token families."""
    user_id = int(user["user_id"])
    ensure_user_allowed(user_id)
    validate_password(payload.new_password)
    cred_repo = get_user_credential_repository()

    record = await cred_repo.async_get_by_user_id(user_id)
    if record is None:
        # No credential exists for this user -- return 401, not 404, so we
        # don't disclose whether the owner has set up password auth.
        raise _generic_failure()

    matched, _ = verify_password(
        payload.current_password,
        record["password_hash"],
        record.get("pepper_version", 1),
    )
    if not matched:
        raise _generic_failure()

    new_phc, new_version = hash_password(payload.new_password)
    await cred_repo.async_update_password_hash(
        record["id"], password_hash=new_phc, pepper_version=new_version
    )

    active_rows = await auth_repo.async_list_active_family_records_for_user(user_id)
    family_records = [_to_family_record(r) for r in active_rows]
    family_ids = TokenFamilyPolicy.family_ids_for_user(family_records)
    revoked_count = 0
    for fid in family_ids:
        hashes = await auth_repo.async_revoke_family(fid, owner_user_id=user_id)
        revoked_count += len(hashes)

    logger.info(
        "credentials_password_changed",
        extra={
            "user_id": user_id,
            "revoked_families": len(family_ids),
            "revoked_tokens": revoked_count,
        },
    )
    return success_response({"message": "Password changed successfully"})
