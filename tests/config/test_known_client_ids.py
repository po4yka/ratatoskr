"""Tests for the KNOWN_CLIENT_IDS registry and validate_client_id integration.

Locks three properties:
  (a) POSITIVE  -- every id in KNOWN_CLIENT_IDS passes validate_client_id when
                   that id is present in the allowlist.
  (b) FORMAT    -- every id in KNOWN_CLIENT_IDS satisfies the format rules
                   enforced by validate_client_id (non-empty, <=100 chars,
                   only alnum + - _ . characters).
  (c) NEGATIVE  -- an unregistered id is rejected with AuthorizationError when
                   the allowlist is set to KNOWN_CLIENT_IDS.
"""

from __future__ import annotations

import pytest

from app.api.exceptions import AuthorizationError
from app.api.routers.auth import tokens
from app.config.known_client_ids import KNOWN_CLIENT_IDS


# ---------------------------------------------------------------------------
# (a) POSITIVE: every shipped id is accepted when in the allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_id", sorted(KNOWN_CLIENT_IDS))
def test_known_client_id_accepted_when_in_allowlist(
    client_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """validate_client_id must not raise for any id in KNOWN_CLIENT_IDS."""
    from app.config import settings

    monkeypatch.setenv("ALLOWED_CLIENT_IDS", ",".join(sorted(KNOWN_CLIENT_IDS)))
    settings.clear_config_cache()

    result = tokens.validate_client_id(client_id)
    assert result is None


# ---------------------------------------------------------------------------
# (b) FORMAT: every id meets the character + length rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_id", sorted(KNOWN_CLIENT_IDS))
def test_known_client_id_format_is_valid(client_id: str) -> None:
    """Every id in KNOWN_CLIENT_IDS must be non-empty, <=100 chars, and
    contain only alphanumeric characters plus - _ ."""
    assert len(client_id) > 0, f"{client_id!r} must not be empty"
    assert len(client_id) <= 100, f"{client_id!r} exceeds 100 characters"
    assert all(
        c.isalnum() or c in "-_." for c in client_id
    ), f"{client_id!r} contains disallowed characters"


# ---------------------------------------------------------------------------
# (c) NEGATIVE: an unregistered id is rejected with AuthorizationError
# ---------------------------------------------------------------------------


def test_unregistered_client_id_raises_authorization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """validate_client_id must raise AuthorizationError for an id that is
    syntactically valid but not present in the allowlist."""
    from app.config import settings

    monkeypatch.setenv("ALLOWED_CLIENT_IDS", ",".join(sorted(KNOWN_CLIENT_IDS)))
    settings.clear_config_cache()

    with pytest.raises(AuthorizationError):
        tokens.validate_client_id("ratatoskr-android-v9.9-unregistered")
