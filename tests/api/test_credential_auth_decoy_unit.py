"""Unit tests for timing-parity decoy verify in credential_auth.

Covers:
- _get_decoy_phc() produces a valid argon2id PHC string whose pre-image is
  a 64-char hex sentinel, matching the output shape of _pre_hash.  This
  ensures argon2 receives the same input length in the decoy path as it
  does in the real verify path so timing parity is preserved.
- run_decoy_verify feeds hasher.verify the same 64-char pre-hashed digest
  shape as the real verify_password path (i.e., HMAC-SHA256(password, pepper)
  -> hexdigest -> argon2.verify).
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import MagicMock, patch

import pytest

from app.api.routers.auth import credential_auth


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PEPPER = "p" * 32  # 32-char test pepper, same as _configure_env in sibling tests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spy_hasher(real_hasher) -> tuple[MagicMock, list[tuple]]:
    """Return a (mock_hasher, captured_calls) pair for inspecting verify args.

    PasswordHasher.verify is a C-extension slot and cannot be patched on an
    instance with patch.object.  We instead replace _get_hasher()'s return
    value with a MagicMock whose .verify records arguments and then delegates
    to the real C implementation so argon2 still does genuine work.
    """
    captured: list[tuple] = []

    def _verify(phc: str, digest: str) -> None:
        captured.append((phc, digest))
        real_hasher.verify(phc, digest)

    mock_hasher = MagicMock(spec=real_hasher)
    mock_hasher.verify.side_effect = _verify
    return mock_hasher, captured


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _configure_env_and_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimum env required for credential_auth and reset module caches."""
    monkeypatch.setenv("CREDENTIALS_LOGIN_PEPPER", PEPPER)
    # Speed argon2 down to its absolute minimum for the unit test suite.
    monkeypatch.setenv("CREDENTIALS_LOGIN_ARGON2_TIME_COST", "1")
    monkeypatch.setenv("CREDENTIALS_LOGIN_ARGON2_MEMORY_KIB", "8192")
    monkeypatch.setenv("CREDENTIALS_LOGIN_ARGON2_PARALLELISM", "1")
    # Other required env vars (config loading is lenient with allow_stub_telegram=True).
    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "test_api_hash_placeholder_value___")
    monkeypatch.setenv("BOT_TOKEN", "1000000000:TESTTOKENPLACEHOLDER1234567890ABC")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "dummy-firecrawl-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-openrouter-key")
    monkeypatch.setenv("ALLOWED_USER_IDS", "123456789")
    # Reset module-level lazy-init caches so the new env wins.
    credential_auth._cfg_holder[0] = None
    credential_auth._hasher_holder[0] = None
    credential_auth._decoy_phc_holder[0] = None


# ---------------------------------------------------------------------------
# _get_decoy_phc: PHC string shape and pre-image length
# ---------------------------------------------------------------------------


def test_decoy_phc_is_argon2id_string() -> None:
    """_get_decoy_phc() must return a PHC string in argon2id format.

    The PHC format starts with '$argon2id$' and is what argon2's
    PasswordHasher.verify() accepts.  A non-PHC string (e.g. a raw hex
    digest) would cause verify() to raise InvalidHashError rather than doing
    real argon2 work, destroying timing parity.
    """
    phc = credential_auth._get_decoy_phc()
    assert phc.startswith("$argon2id$"), f"Expected argon2id PHC format, got: {phc!r}"


def test_decoy_phc_preimage_is_64_hex_chars() -> None:
    """The decoy PHC must be the hash of exactly 64 hex-range characters.

    _pre_hash returns hmac-sha256 hexdigest -- always 64 lowercase hex chars.
    The decoy PHC is produced from '0' * 64, which has the same length and
    character class.  If the pre-image length ever changes, the argon2 work
    factor changes (argon2 hashes variable-length inputs), breaking timing
    parity with the real verify path.
    """
    sentinel = "0" * 64
    assert len(sentinel) == 64
    assert all(c in "0123456789abcdef" for c in sentinel)

    hasher = credential_auth._get_hasher()
    phc = credential_auth._get_decoy_phc()
    # The PHC was produced by hashing sentinel; verify must succeed.
    try:
        hasher.verify(phc, sentinel)
        verified = True
    except Exception:
        verified = False

    assert verified, "Decoy PHC was not produced from the '0'*64 sentinel"


def test_decoy_phc_is_cached() -> None:
    """_get_decoy_phc() must return the same object on repeated calls (lazy cache)."""
    phc_a = credential_auth._get_decoy_phc()
    phc_b = credential_auth._get_decoy_phc()
    assert phc_a is phc_b, "Decoy PHC should be computed once and cached"


# ---------------------------------------------------------------------------
# run_decoy_verify: input shape parity with real verify_password
# ---------------------------------------------------------------------------


def test_run_decoy_verify_passes_64_char_digest_to_hasher() -> None:
    """run_decoy_verify must call hasher.verify with a 64-char hex digest.

    The real verify_password path is:
        digest = _pre_hash(password, pepper)   # 64-char hex
        hasher.verify(phc, digest)

    run_decoy_verify must follow the same shape so argon2 receives a 64-char
    input in both cases and timing parity is preserved.
    """
    password = "some-test-password"
    real_hasher = credential_auth._get_hasher()
    mock_hasher, captured = _make_spy_hasher(real_hasher)

    with patch.object(credential_auth, "_get_hasher", return_value=mock_hasher):
        credential_auth.run_decoy_verify(password)

    assert len(captured) == 1, "hasher.verify must be called exactly once"
    _phc, digest = captured[0]

    # The digest must be a 64-char lowercase hex string -- identical in length
    # and character class to _pre_hash output.
    assert len(digest) == 64, f"Expected 64-char digest, got {len(digest)}: {digest!r}"
    assert all(c in "0123456789abcdef" for c in digest), (
        f"Digest must be lowercase hex, got: {digest!r}"
    )


def test_run_decoy_verify_digest_matches_pre_hash() -> None:
    """run_decoy_verify must produce the same digest as verify_password would.

    Both paths must compute HMAC-SHA256(password, pepper).hexdigest() before
    calling argon2.  If run_decoy_verify skipped the pre-hash step (or used
    a different key), an attacker could distinguish the decoy path from the
    real path by timing differences (argon2 cost is input-length-sensitive).
    """
    password = "hunter2"
    expected_digest = hmac.new(PEPPER.encode(), password.encode(), hashlib.sha256).hexdigest()
    assert len(expected_digest) == 64

    real_hasher = credential_auth._get_hasher()
    mock_hasher, captured = _make_spy_hasher(real_hasher)

    with patch.object(credential_auth, "_get_hasher", return_value=mock_hasher):
        credential_auth.run_decoy_verify(password)

    assert captured, "hasher.verify was not called"
    _phc, actual_digest = captured[0]
    assert actual_digest == expected_digest, (
        f"run_decoy_verify passed digest {actual_digest!r}, "
        f"expected {expected_digest!r} (HMAC-SHA256 of password+pepper)"
    )


def test_run_decoy_verify_phc_arg_is_decoy() -> None:
    """run_decoy_verify must pass _get_decoy_phc() as the first arg to hasher.verify."""
    password = "some-password"
    expected_phc = credential_auth._get_decoy_phc()

    real_hasher = credential_auth._get_hasher()
    mock_hasher, captured = _make_spy_hasher(real_hasher)

    with patch.object(credential_auth, "_get_hasher", return_value=mock_hasher):
        credential_auth.run_decoy_verify(password)

    assert captured, "hasher.verify was not called"
    actual_phc, _digest = captured[0]
    assert actual_phc == expected_phc, (
        "run_decoy_verify must pass the precomputed decoy PHC to hasher.verify"
    )


def test_run_decoy_verify_always_swallows_exception() -> None:
    """run_decoy_verify must never raise, even if the hasher raises unexpectedly.

    The caller relies on the exception being swallowed; any propagation would
    change the response shape and leak the 'no such user' vs 'wrong password'
    distinction.
    """
    mock_hasher = MagicMock()
    mock_hasher.verify.side_effect = RuntimeError("unexpected error")

    with patch.object(credential_auth, "_get_hasher", return_value=mock_hasher):
        # Must not raise.
        credential_auth.run_decoy_verify("any-password")
