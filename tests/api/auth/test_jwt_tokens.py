"""Unit tests for JWT token creation/validation and client_id allowlist enforcement.

Covers:
- decode_token: valid round-trip, tampered signature, alg=none rejection, expired
  token, wrong type, access-token used as refresh
- validate_client_id: missing, bad format, blocked by allowlist, allowed by
  allowlist, empty-allowlist accepts all
- verify_telegram_webapp_init_data: valid HMAC, tampered hash, expired auth_date,
  future auth_date, unauthorized user, empty/missing fields
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import jwt
import pytest

from app.api.exceptions import (
    AuthenticationError,
    AuthorizationError,
    TokenExpiredError,
    TokenInvalidError,
    TokenWrongTypeError,
    ValidationError,
)
from app.api.routers.auth import tokens as tokens_module
from app.api.routers.auth.tokens import (
    ALGORITHM,
    JWT_AUDIENCE,
    JWT_ISSUER,
    JWT_LEGACY_CLAIMS_GRACE_SECONDS,
    create_access_token,
    decode_token,
    validate_client_id,
)
from app.api.routers.auth.webapp_auth import verify_telegram_webapp_init_data
from app.config import clear_config_cache
from app.core.time_utils import UTC

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JWT_SECRET = "test-secret-key-32-characters-long-123456"
_JWT_PREVIOUS_SECRET = "p" * 40
_BOT_TOKEN = "123456789:test-token-secret-part-at-least-30-chars"
_ALLOWED_USER_ID = 123456789


def _configure_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point decode_token at our test secret and reset the lazy cache."""
    monkeypatch.setenv("JWT_SECRET_KEY", _JWT_SECRET)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("JWT_SECRET_PREVIOUS_KEYS", raising=False)
    # _load_secret_key now reads the validated AppConfig field, so the cached
    # config must be rebuilt from this test's env rather than whatever a prior
    # test left behind.
    clear_config_cache()
    tokens_module._secret_key_holder[0] = None
    tokens_module._previous_secret_keys_holder[0] = None
    tokens_module._jwt_legacy_claim_grace_started_at_holder[0] = datetime.now(UTC)


def _configure_allowlist(monkeypatch: pytest.MonkeyPatch, client_ids: str) -> None:
    """Set ALLOWED_CLIENT_IDS and reset the warned-once flag."""
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", client_ids)
    tokens_module._allowlist_empty_warned_holder[0] = False


def test_jwt_secret_alias_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting only the documented JWT_SECRET alias (not JWT_SECRET_KEY) must
    configure signing/verification via the validated AppConfig field."""
    alias_secret = "alias-secret-key-32-characters-long-abc789"
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.setenv("JWT_SECRET", alias_secret)
    monkeypatch.delenv("JWT_SECRET_PREVIOUS_KEYS", raising=False)
    clear_config_cache()
    tokens_module._secret_key_holder[0] = None
    tokens_module._previous_secret_keys_holder[0] = None
    try:
        # The alias value flows through to signing/verification unchanged.
        assert tokens_module._get_secret_key() == alias_secret

        token = create_access_token(user_id=1, client_id="test-client")
        payload = decode_token(token, expected_type="access")
        assert payload["user_id"] == 1

        # A token signed with a different secret must not verify.
        foreign = jwt.encode(
            {
                "user_id": 1,
                "type": "access",
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=5),
                "iss": JWT_ISSUER,
                "aud": JWT_AUDIENCE,
            },
            "some-other-secret-32-characters-long-000",
            algorithm=ALGORITHM,
        )
        with pytest.raises(TokenInvalidError):
            decode_token(foreign, expected_type="access")
    finally:
        clear_config_cache()
        tokens_module._secret_key_holder[0] = None


def _build_init_data(
    user_id: int = _ALLOWED_USER_ID,
    username: str = "testuser",
    auth_date: int | None = None,
    bot_token: str = _BOT_TOKEN,
    *,
    tamper_hash: bool = False,
) -> str:
    """Build a correctly-signed (or deliberately tampered) Telegram initData string."""
    if auth_date is None:
        auth_date = int(time.time())

    user_payload = json.dumps({"id": user_id, "username": username, "first_name": "Test"})
    params: dict[str, str] = {
        "auth_date": str(auth_date),
        "user": user_payload,
    }

    # Build data-check-string exactly as the production validator does
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if tamper_hash:
        # Flip one hex digit so the signature is invalid
        computed = ("0" if computed[0] != "0" else "1") + computed[1:]

    params["hash"] = computed
    return urlencode(params)


# ===========================================================================
# decode_token — access token round-trip
# ===========================================================================


def test_decode_valid_access_token_returns_payload(monkeypatch):
    _configure_jwt(monkeypatch)
    token = create_access_token(user_id=42, username="alice", client_id="mobile-ios")
    payload = decode_token(token, expected_type="access")
    assert payload["user_id"] == 42
    assert payload["username"] == "alice"
    assert payload["client_id"] == "mobile-ios"
    assert payload["type"] == "access"
    assert payload["iss"] == JWT_ISSUER
    assert payload["aud"] == JWT_AUDIENCE


def test_decode_valid_access_token_without_expected_type(monkeypatch):
    _configure_jwt(monkeypatch)
    token = create_access_token(user_id=7)
    payload = decode_token(token)
    assert payload["user_id"] == 7


# ===========================================================================
# decode_token — tampered signature is rejected
# ===========================================================================


def test_decode_raises_token_invalid_for_tampered_signature(monkeypatch):
    _configure_jwt(monkeypatch)
    token = create_access_token(user_id=1)
    # Replace last few chars so the HMAC no longer verifies
    tampered = token[:-4] + ("XXXX" if not token.endswith("XXXX") else "YYYY")
    with pytest.raises(TokenInvalidError):
        decode_token(tampered)


def test_decode_accepts_token_signed_with_previous_secret(monkeypatch):
    _configure_jwt(monkeypatch)
    monkeypatch.setenv("JWT_SECRET_PREVIOUS_KEYS", _JWT_PREVIOUS_SECRET)
    tokens_module._previous_secret_keys_holder[0] = None
    payload = {
        "user_id": 24,
        "type": "access",
        "exp": datetime.now(UTC) + timedelta(minutes=30),
        "iat": datetime.now(UTC),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "jti": "previous-key-token",
    }
    token = jwt.encode(payload, _JWT_PREVIOUS_SECRET, algorithm=ALGORITHM)

    decoded = decode_token(token, expected_type="access")

    assert decoded["user_id"] == 24


def test_new_tokens_are_signed_with_primary_secret_during_rotation(monkeypatch):
    _configure_jwt(monkeypatch)
    monkeypatch.setenv("JWT_SECRET_PREVIOUS_KEYS", _JWT_PREVIOUS_SECRET)
    tokens_module._previous_secret_keys_holder[0] = None

    token = create_access_token(user_id=31)

    assert (
        jwt.decode(
            token,
            _JWT_SECRET,
            algorithms=[ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
        )["user_id"]
        == 31
    )
    with pytest.raises(jwt.InvalidTokenError):
        jwt.decode(token, _JWT_PREVIOUS_SECRET, algorithms=[ALGORITHM])


def test_decode_rejects_malformed_previous_secret(monkeypatch):
    _configure_jwt(monkeypatch)
    monkeypatch.setenv("JWT_SECRET_PREVIOUS_KEYS", "too-short")
    tokens_module._previous_secret_keys_holder[0] = None

    token = create_access_token(user_id=1)

    with pytest.raises(RuntimeError, match="JWT_SECRET_PREVIOUS_KEYS"):
        decode_token(token)


# ===========================================================================
# decode_token — alg=none attack is rejected
# ===========================================================================


def test_decode_rejects_alg_none_token(monkeypatch):
    """A JWT with alg=none must be rejected even if the payload is otherwise valid."""
    _configure_jwt(monkeypatch)
    # Craft a token signed with alg=none (unsigned)
    payload = {
        "user_id": 1,
        "type": "access",
        "exp": datetime.now(UTC) + timedelta(hours=1),
        "iat": datetime.now(UTC),
        "jti": "test-jti",
    }
    # PyJWT will raise if you try to encode alg=none, so we hand-craft the raw JWT
    import base64

    def _b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body_data = {
        "user_id": 1,
        "type": "access",
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "iat": int(datetime.now(UTC).timestamp()),
        "jti": "test-jti",
    }
    body = _b64(json.dumps(body_data).encode())
    unsigned_token = f"{header}.{body}."

    with pytest.raises(TokenInvalidError):
        decode_token(unsigned_token)


# ===========================================================================
# decode_token — expired token
# ===========================================================================


def test_decode_raises_token_expired_for_past_exp(monkeypatch):
    _configure_jwt(monkeypatch)
    # Build a token that expired 1 second ago
    payload = {
        "user_id": 99,
        "type": "access",
        "exp": datetime.now(UTC) - timedelta(seconds=1),
        "iat": datetime.now(UTC) - timedelta(minutes=31),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "jti": "expired-jti",
    }
    expired_token = jwt.encode(payload, _JWT_SECRET, algorithm=ALGORITHM)
    with pytest.raises(TokenExpiredError):
        decode_token(expired_token, expected_type="access")


# ===========================================================================
# decode_token — wrong type raises TokenWrongTypeError
# ===========================================================================


def test_decode_raises_wrong_type_when_access_token_used_as_refresh(monkeypatch):
    _configure_jwt(monkeypatch)
    token = create_access_token(user_id=5)
    with pytest.raises(TokenWrongTypeError):
        decode_token(token, expected_type="refresh")


def test_decode_raises_wrong_type_when_refresh_payload_used_as_access(monkeypatch):
    _configure_jwt(monkeypatch)
    # Craft a token whose `type` field says "refresh" but we expect "access"
    payload = {
        "user_id": 5,
        "type": "refresh",
        "exp": datetime.now(UTC) + timedelta(days=30),
        "iat": datetime.now(UTC),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "jti": "test-jti",
    }
    refresh_shaped_token = jwt.encode(payload, _JWT_SECRET, algorithm=ALGORITHM)
    with pytest.raises(TokenWrongTypeError):
        decode_token(refresh_shaped_token, expected_type="access")


def _strict_claim_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "user_id": 42,
        "type": "access",
        "exp": datetime.now(UTC) + timedelta(minutes=30),
        "iat": datetime.now(UTC),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "jti": "strict-claims-token",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("claim", ["exp", "iat", "type", "user_id"])
def test_decode_rejects_token_missing_required_core_claim(
    monkeypatch: pytest.MonkeyPatch,
    claim: str,
) -> None:
    _configure_jwt(monkeypatch)
    payload = _strict_claim_payload()
    payload.pop(claim)
    token = jwt.encode(payload, _JWT_SECRET, algorithm=ALGORITHM)

    with pytest.raises(TokenInvalidError):
        decode_token(token, expected_type="access")


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("aud", "wrong-audience"),
        ("iss", "wrong-issuer"),
    ],
)
def test_decode_rejects_wrong_audience_or_issuer(
    monkeypatch: pytest.MonkeyPatch,
    claim: str,
    value: str,
) -> None:
    _configure_jwt(monkeypatch)
    payload = _strict_claim_payload(**{claim: value})
    token = jwt.encode(payload, _JWT_SECRET, algorithm=ALGORITHM)

    with pytest.raises(TokenInvalidError):
        decode_token(token, expected_type="access")


def test_decode_accepts_legacy_token_missing_audience_and_issuer_during_grace(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_jwt(monkeypatch)
    payload = _strict_claim_payload()
    payload.pop("aud")
    payload.pop("iss")
    token = jwt.encode(payload, _JWT_SECRET, algorithm=ALGORITHM)

    decoded = decode_token(token, expected_type="access")

    assert decoded["user_id"] == 42
    assert any(record.message == "jwt_legacy_missing_aud_iss_accepted" for record in caplog.records)


def test_decode_rejects_token_with_stripped_audience_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_jwt(monkeypatch)
    tokens_module._jwt_legacy_claim_grace_started_at_holder[0] = datetime.now(UTC) - timedelta(
        seconds=JWT_LEGACY_CLAIMS_GRACE_SECONDS + 1
    )
    payload = _strict_claim_payload()
    payload.pop("aud")
    token = jwt.encode(payload, _JWT_SECRET, algorithm=ALGORITHM)

    with pytest.raises(TokenInvalidError):
        decode_token(token, expected_type="access")


# ===========================================================================
# validate_client_id — allowlist enforcement
# ===========================================================================


def test_validate_client_id_raises_validation_error_when_missing(monkeypatch):
    _configure_allowlist(monkeypatch, "com.example.app")
    with pytest.raises(ValidationError):
        validate_client_id(None)


def test_validate_client_id_raises_validation_error_for_empty_string(monkeypatch):
    _configure_allowlist(monkeypatch, "com.example.app")
    with pytest.raises(ValidationError):
        validate_client_id("")


def test_validate_client_id_raises_authorization_error_for_blocked_client(monkeypatch):
    _configure_allowlist(monkeypatch, "com.example.allowed")
    with pytest.raises(AuthorizationError):
        validate_client_id("com.attacker.evil")


def test_validate_client_id_passes_for_allowed_client(monkeypatch):
    _configure_allowlist(monkeypatch, "com.example.app")
    # Should not raise
    validate_client_id("com.example.app")


def test_validate_client_id_passes_for_any_client_when_allowlist_empty(monkeypatch):
    """Empty ALLOWED_CLIENT_IDS means no restriction — any valid-format client passes."""
    _configure_allowlist(monkeypatch, "")
    # Should not raise for any well-formed client_id
    validate_client_id("com.anything.goes")


def test_validate_client_id_raises_validation_error_for_bad_format(monkeypatch):
    """client_id with characters outside [A-Za-z0-9-_.] is rejected regardless of allowlist."""
    _configure_allowlist(monkeypatch, "")
    with pytest.raises(ValidationError):
        validate_client_id("bad client id!")


def test_validate_client_id_raises_validation_error_for_too_long(monkeypatch):
    _configure_allowlist(monkeypatch, "")
    with pytest.raises(ValidationError):
        validate_client_id("a" * 101)


# ===========================================================================
# verify_telegram_webapp_init_data — HMAC validation
# ===========================================================================


def test_webapp_valid_init_data_returns_user_info(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", _BOT_TOKEN)
    monkeypatch.setenv("ALLOWED_USER_IDS", str(_ALLOWED_USER_ID))
    from app.config import Config

    Config.clear_cache() if hasattr(Config, "clear_cache") else None

    init_data = _build_init_data()
    result = verify_telegram_webapp_init_data(init_data)
    assert result["user_id"] == _ALLOWED_USER_ID
    assert result["username"] == "testuser"


def test_webapp_tampered_hash_raises_authentication_error(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", _BOT_TOKEN)
    monkeypatch.setenv("ALLOWED_USER_IDS", str(_ALLOWED_USER_ID))

    init_data = _build_init_data(tamper_hash=True)
    with pytest.raises(AuthenticationError):
        verify_telegram_webapp_init_data(init_data)


def test_webapp_expired_auth_date_raises_authentication_error(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", _BOT_TOKEN)
    monkeypatch.setenv("ALLOWED_USER_IDS", str(_ALLOWED_USER_ID))
    # auth_date more than 15 min + 1 min skew = 16 min ago
    stale = int(time.time()) - (16 * 60 + 1)
    init_data = _build_init_data(auth_date=stale)
    with pytest.raises(AuthenticationError):
        verify_telegram_webapp_init_data(init_data)


def test_webapp_future_auth_date_raises_authentication_error(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", _BOT_TOKEN)
    monkeypatch.setenv("ALLOWED_USER_IDS", str(_ALLOWED_USER_ID))
    # auth_date > 1 min in the future (beyond clock-skew tolerance)
    future = int(time.time()) + 120
    init_data = _build_init_data(auth_date=future)
    with pytest.raises(AuthenticationError):
        verify_telegram_webapp_init_data(init_data)


def test_webapp_unauthorized_user_raises_authorization_error(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", _BOT_TOKEN)
    # Only allow a different user
    monkeypatch.setenv("ALLOWED_USER_IDS", "999999999")

    init_data = _build_init_data(user_id=_ALLOWED_USER_ID)
    with pytest.raises(AuthorizationError):
        verify_telegram_webapp_init_data(init_data)


def test_webapp_empty_init_data_raises_authentication_error(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", _BOT_TOKEN)
    monkeypatch.setenv("ALLOWED_USER_IDS", str(_ALLOWED_USER_ID))
    with pytest.raises(AuthenticationError):
        verify_telegram_webapp_init_data("")


def test_webapp_missing_hash_raises_authentication_error(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", _BOT_TOKEN)
    monkeypatch.setenv("ALLOWED_USER_IDS", str(_ALLOWED_USER_ID))
    # Valid-looking data but no hash field
    init_data = f"auth_date={int(time.time())}&user=%7B%22id%22%3A123%7D"
    with pytest.raises(AuthenticationError):
        verify_telegram_webapp_init_data(init_data)


def test_webapp_wrong_bot_token_raises_authentication_error(monkeypatch):
    """Signature computed with the correct token must fail against a wrong token."""
    monkeypatch.setenv("ALLOWED_USER_IDS", str(_ALLOWED_USER_ID))
    # Produce valid initData with the real bot token
    init_data = _build_init_data(bot_token=_BOT_TOKEN)
    # But configure the server with a different token
    monkeypatch.setenv("BOT_TOKEN", "987654321:wrong-token-that-is-definitely-not-right")
    with pytest.raises(AuthenticationError):
        verify_telegram_webapp_init_data(init_data)
