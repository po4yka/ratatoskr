"""Apple Sign-In identity provider endpoints."""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import jwt
from starlette.responses import Response  # noqa: TC002 - FastAPI resolves this annotation

from app.api.dependencies.database import get_session_manager
from app.api.exceptions import AuthenticationError, ConfigurationError
from app.api.models.auth import (
    AppleSignInCallbackRequest,
    AppleSignInStartRequest,
    AppleSignInStartResponse,
)
from app.api.models.responses import success_response
from app.api.routers.auth._fastapi import APIRouter
from app.api.routers.auth.credential_auth import canonicalize_email, ensure_user_allowed
from app.api.routers.auth.identity_tokens import issue_auth_tokens
from app.api.routers.auth.tokens import JWT_AUDIENCE, JWT_ISSUER, validate_client_id
from app.config import load_config
from app.infrastructure.persistence.repositories.user_identity_repository import (
    UserIdentityRepository,
)

router = APIRouter()

_APPLE_AUTHORIZE_URL = "https://appleid.apple.com/auth/authorize"
_APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
_APPLE_ISSUER = "https://appleid.apple.com"


@router.post("/apple/start")
async def start_apple_sign_in(payload: AppleSignInStartRequest) -> Any:
    """Build Apple Sign-In authorization parameters for the client."""
    validate_client_id(payload.client_id)
    cfg = load_config(allow_stub_telegram=True)
    apple_client_id = _require_apple_client_id(cfg.auth.apple_client_id)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(48)
    code_challenge = _pkce_challenge(code_verifier)
    params = {
        "response_type": "code id_token",
        "response_mode": "form_post",
        "client_id": apple_client_id,
        "redirect_uri": payload.redirect_uri,
        "scope": payload.scope,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return success_response(
        AppleSignInStartResponse(
            authorization_url=f"{_APPLE_AUTHORIZE_URL}?{urlencode(params)}",
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            code_challenge=code_challenge,
        )
    )


@router.post("/apple/callback")
async def apple_callback(payload: AppleSignInCallbackRequest, response: Response) -> Any:
    """Validate an Apple id_token and issue Ratatoskr JWTs."""
    validate_client_id(payload.client_id)
    cfg = load_config(allow_stub_telegram=True)
    apple_client_id = _require_apple_client_id(cfg.auth.apple_client_id)
    claims = _validate_apple_id_token(
        payload.id_token,
        audience=apple_client_id,
        nonce=payload.nonce,
    )
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise AuthenticationError("Apple identity token is missing subject")

    repo = UserIdentityRepository(get_session_manager())
    existing = await repo.async_get_identity(provider="apple", subject=subject)
    if existing is not None:
        user_id = int(existing["user_id"])
    else:
        email = _claim_email(claims)
        _display_email, email_canonical = canonicalize_email(email)
        if not email_canonical:
            raise AuthenticationError("Apple identity is not linked to a Ratatoskr user")
        user_id = await repo.async_find_user_id_by_email(email_canonical)
        if user_id is None:
            raise AuthenticationError("Apple identity is not linked to a Ratatoskr user")

    ensure_user_allowed(user_id)
    email = _claim_email(claims)
    display_email, email_canonical = canonicalize_email(email)
    await repo.async_upsert_identity(
        user_id=user_id,
        provider="apple",
        subject=subject,
        email=display_email,
        email_canonical=email_canonical,
        email_verified=_claim_bool(claims.get("email_verified")),
        display_name=None,
    )
    return await issue_auth_tokens(
        user_id=user_id,
        username=None,
        client_id=payload.client_id,
        response=response,
    )


def _require_apple_client_id(value: str | None) -> str:
    client_id = (value or "").strip()
    if not client_id:
        raise ConfigurationError(
            "Apple Sign-In is not configured. Set APPLE_SIGNIN_CLIENT_ID.",
            config_key="APPLE_SIGNIN_CLIENT_ID",
        )
    return client_id


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _validate_apple_id_token(
    token: str,
    *,
    audience: str,
    nonce: str | None,
) -> dict[str, Any]:
    try:
        signing_key = jwt.PyJWKClient(_APPLE_JWKS_URL).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=_APPLE_ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid Apple identity token") from exc
    if nonce is not None and claims.get("nonce") != nonce:
        raise AuthenticationError("Invalid Apple identity token nonce")
    return claims


def _claim_email(claims: dict[str, Any]) -> str | None:
    value = claims.get("email")
    return str(value).strip() if value else None


def _claim_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


__all__ = [
    "JWT_AUDIENCE",
    "JWT_ISSUER",
    "apple_callback",
    "router",
    "start_apple_sign_in",
]
