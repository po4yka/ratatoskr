"""Nickname/email + password authentication: hashing, identifier canonicalization,
lockout helpers, and the timing-parity decoy verify path.

Independent of ``secret_auth.py`` (machine-client secret-key flow). Uses
argon2id with an HMAC-SHA256 pre-hash that mixes in CREDENTIALS_LOGIN_PEPPER
so a DB leak alone does not expose passwords. The pepper MUST be different
from JWT_SECRET_KEY and SECRET_LOGIN_PEPPER -- separate secrets in separate
domains, validated at config load time.
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
from typing import Any, Literal

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

from app.api.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ValidationError,
)
from app.config import AppConfig, Config, load_config
from app.core.logging_utils import get_logger

logger = get_logger(__name__)

# Surface the canonical "Invalid credentials" string in one place so the
# router can never accidentally diverge between "no such user", "wrong
# password", and "exhausted lockout". Anti-enumeration depends on this.
GENERIC_AUTH_FAILURE_MESSAGE = "Invalid credentials"

# Bumped only when password storage format changes (e.g., new pepper rotation
# scheme). Persisted alongside each row's hash so old peppers can verify in
# a future rotation window.
CURRENT_PEPPER_VERSION = 1

# Single-element holders so the lazy-init sites do not need `global`.
_cfg_holder: list[AppConfig | None] = [None]
_hasher_holder: list[PasswordHasher | None] = [None]
_decoy_phc_holder: list[str | None] = [None]


def _get_cfg() -> AppConfig:
    if _cfg_holder[0] is None:
        _cfg_holder[0] = load_config(allow_stub_telegram=True)
    return _cfg_holder[0]


def _get_auth_config() -> Any:
    return _get_cfg().auth


def _build_hasher() -> PasswordHasher:
    auth = _get_auth_config()
    return PasswordHasher(
        time_cost=auth.credentials_argon2_time_cost,
        memory_cost=auth.credentials_argon2_memory_kib,
        parallelism=auth.credentials_argon2_parallelism,
    )


def _get_hasher() -> PasswordHasher:
    if _hasher_holder[0] is None:
        _hasher_holder[0] = _build_hasher()
    return _hasher_holder[0]


def _get_decoy_phc() -> str:
    """Return a precomputed PHC string for timing-parity verifies.

    When the identifier doesn't resolve to a user (or the user is not in
    ALLOWED_USER_IDS), we still want the wall-clock cost of an argon2 verify
    so an attacker can't differentiate "no such user" from "wrong password"
    by timing alone.

    The decoy PHC is hashed from a 64-char hex sentinel that matches the
    output format of _pre_hash so argon2 receives the same input length and
    shape as in the real verify path. Hashing a raw variable-length string
    would break timing parity because argon2 cost varies with input length.
    """
    if _decoy_phc_holder[0] is None:
        # 64 zero-chars mimic the hex-digest output of _pre_hash exactly.
        _decoy_phc_holder[0] = _get_hasher().hash("0" * 64)
    return _decoy_phc_holder[0]


def _get_credentials_pepper(version: int = CURRENT_PEPPER_VERSION) -> str:
    """Resolve the credentials pepper for a given pepper_version.

    Pepper presence is the only gate: deploys that haven't set
    CREDENTIALS_LOGIN_PEPPER will surface a 503 ConfigurationError on the
    first credentials-login request, but the rest of the API still boots.
    Future rotations will keep the previous pepper readable until all stored
    rows have been migrated via opportunistic rehash (see verify_password's
    needs_rehash hint).
    """
    cfg = _get_cfg()
    pepper = cfg.auth.credentials_pepper
    if not pepper:
        raise ConfigurationError(
            "Credentials login is not configured on this deployment. "
            "Set CREDENTIALS_LOGIN_PEPPER (>=32 chars) to enable it.",
            config_key="CREDENTIALS_LOGIN_PEPPER",
        )
    if version != CURRENT_PEPPER_VERSION:
        # Reserved for a future rotation: keep the old pepper around in
        # CREDENTIALS_LOGIN_PEPPER_V<n> env vars and resolve here.
        raise ConfigurationError(
            f"Unknown credentials pepper version: {version}",
            config_key=f"CREDENTIALS_LOGIN_PEPPER_V{version}",
        )
    return pepper


def ensure_user_allowed(user_id: int) -> None:
    """Raise AuthorizationError if user is not in the owner allowlist."""
    if not Config.is_user_allowed(user_id, fail_open_when_empty=False):
        logger.warning(
            "credentials_login_user_not_authorized",
            extra={"user_id": user_id},
        )
        raise AuthorizationError(GENERIC_AUTH_FAILURE_MESSAGE)


IdentifierKind = Literal["nickname", "email"]


def canonicalize_identifier(identifier: str) -> tuple[IdentifierKind, str, str]:
    """Normalize a raw identifier into (kind, display, canonical).

    - ``@`` presence routes to email; otherwise nickname.
    - Display value preserves the original case (after stripping + NFKC
      normalization) so the owner sees their preferred capitalization.
    - Canonical value is lowercased + NFKC-normalized for indexing and
      uniqueness checks.
    """
    cleaned = unicodedata.normalize("NFKC", identifier).strip()
    if not cleaned:
        raise ValidationError("Identifier must not be empty", details={"field": "identifier"})
    kind: IdentifierKind = "email" if "@" in cleaned else "nickname"
    canonical = cleaned.casefold()
    if kind == "nickname" and len(cleaned) > 64:
        raise ValidationError(
            "Nickname must be at most 64 characters", details={"field": "identifier"}
        )
    if kind == "email" and len(cleaned) > 256:
        raise ValidationError(
            "Email must be at most 256 characters", details={"field": "identifier"}
        )
    return kind, cleaned, canonical


def canonicalize_nickname(nickname: str) -> tuple[str, str]:
    """Normalize a nickname into (display, canonical). Used by the bootstrap CLI."""
    cleaned = unicodedata.normalize("NFKC", nickname).strip()
    if not cleaned:
        raise ValidationError("Nickname must not be empty", details={"field": "nickname"})
    if "@" in cleaned:
        raise ValidationError(
            "Nickname must not contain '@' (use the email field instead)",
            details={"field": "nickname"},
        )
    if len(cleaned) > 64:
        raise ValidationError(
            "Nickname must be at most 64 characters", details={"field": "nickname"}
        )
    return cleaned, cleaned.casefold()


def canonicalize_email(email: str | None) -> tuple[str | None, str | None]:
    """Normalize an email into (display, canonical). None passes through."""
    if email is None:
        return None, None
    cleaned = unicodedata.normalize("NFKC", email).strip()
    if not cleaned:
        return None, None
    if "@" not in cleaned:
        raise ValidationError("Email must contain '@'", details={"field": "email"})
    if len(cleaned) > 256:
        raise ValidationError("Email must be at most 256 characters", details={"field": "email"})
    return cleaned, cleaned.casefold()


def validate_password(password: str) -> str:
    """Validate password length per AuthConfig limits.

    Length-only validation: complexity rules in a single-owner system buy
    little and add UX friction. The owner picks their own password discipline.
    """
    auth = _get_auth_config()
    if not isinstance(password, str):
        raise ValidationError("Password must be a string", details={"field": "password"})
    if len(password) < auth.credentials_password_min_length:
        raise ValidationError(
            f"Password must be at least {auth.credentials_password_min_length} characters",
            details={"field": "password"},
        )
    if len(password) > auth.credentials_password_max_length:
        # DoS guard: argon2 over a 100k-char input will eat the event loop.
        raise ValidationError(
            f"Password must be at most {auth.credentials_password_max_length} characters",
            details={"field": "password"},
        )
    return password


def _pre_hash(password: str, pepper: str) -> str:
    """HMAC-SHA256 pre-hash; argon2 is fed the hex digest, not the raw password.

    Stops a DB-only leak from being a password leak (the attacker still needs
    the pepper) and bounds argon2's input size regardless of password length.
    Hex output keeps the result printable and a stable 64 chars.
    """
    return hmac.new(pepper.encode(), password.encode(), hashlib.sha256).hexdigest()


def hash_password(password: str) -> tuple[str, int]:
    """Hash a password for storage. Returns (PHC string, pepper_version).

    The PHC string carries argon2's salt and cost params inline; do not store
    a separate salt column. ``pepper_version`` lets a future rotation keep
    old hashes verifiable until everyone has logged in once.
    """
    validate_password(password)
    pepper = _get_credentials_pepper()
    digest = _pre_hash(password, pepper)
    return _get_hasher().hash(digest), CURRENT_PEPPER_VERSION


def verify_password(password: str, phc: str, pepper_version: int) -> tuple[bool, bool]:
    """Verify a password against a stored PHC hash.

    Returns ``(matched, needs_rehash)``. ``needs_rehash`` is True when
    argon2 reports the stored hash uses weaker parameters than current
    config -- the caller can then opportunistically rehash on a successful
    login to upgrade the row.
    """
    pepper = _get_credentials_pepper(pepper_version)
    digest = _pre_hash(password, pepper)
    hasher = _get_hasher()
    try:
        hasher.verify(phc, digest)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    return True, hasher.check_needs_rehash(phc)


def run_decoy_verify(password: str) -> None:
    """Burn the wall-clock time of an argon2 verify against a decoy PHC.

    Call when the identifier didn't resolve to a row, OR when the row's
    user_id isn't in ALLOWED_USER_IDS. Without this, an attacker can
    distinguish "no such nickname" from "wrong password" purely by latency.
    The exception is swallowed -- this path always "fails" for the caller.

    The password is pre-hashed with the current credentials pepper before
    being passed to argon2, mirroring the real verify path exactly so that
    argon2 receives the same 64-char hex input in both cases and timing
    parity is preserved.
    """
    try:
        digest = _pre_hash(password, _get_credentials_pepper())
        _get_hasher().verify(_get_decoy_phc(), digest)
    except Exception:
        pass


def lockout_seconds_remaining(locked_until: Any, now: Any) -> int:
    """Return seconds until lockout expiry; 0 if unlocked or already past."""
    if locked_until is None:
        return 0
    delta = locked_until - now
    seconds = int(delta.total_seconds())
    return max(seconds, 0)


def auth_failure() -> AuthenticationError:
    """Single chokepoint for the canonical 401 failure -- callers MUST use this."""
    return AuthenticationError(GENERIC_AUTH_FAILURE_MESSAGE)
