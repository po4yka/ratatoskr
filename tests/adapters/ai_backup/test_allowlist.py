"""Tests for the AI account backup host allowlist guard."""

from __future__ import annotations

import pytest

from app.adapters.ai_backup.allowlist import assert_host_allowed
from app.adapters.ai_backup.errors import AiBackupHostDeniedError


def test_exact_match_passes() -> None:
    assert_host_allowed("https://chatgpt.com/backend-api/x", ["chatgpt.com"])


def test_exact_mismatch_raises() -> None:
    with pytest.raises(AiBackupHostDeniedError):
        assert_host_allowed("https://evil.com/x", ["chatgpt.com"])


@pytest.mark.parametrize(
    "host",
    ["files.oaiusercontent.com", "oaiusercontent.com"],
)
def test_wildcard_matches_subdomain_and_apex(host: str) -> None:
    assert_host_allowed(f"https://{host}/file", ["*.oaiusercontent.com"])


def test_wildcard_does_not_match_lookalike_suffix() -> None:
    with pytest.raises(AiBackupHostDeniedError):
        assert_host_allowed("https://noaiusercontent.com/x", ["*.oaiusercontent.com"])


def test_case_insensitive() -> None:
    assert_host_allowed("https://CHATGPT.COM/x", ["chatgpt.com"])


def test_userinfo_probe_is_rejected() -> None:
    # urlparse resolves the host to chatgpt.com.evil.com, not chatgpt.com.
    with pytest.raises(AiBackupHostDeniedError):
        assert_host_allowed("https://chatgpt.com@evil.com/x", ["chatgpt.com"])
    with pytest.raises(AiBackupHostDeniedError):
        assert_host_allowed("https://user@chatgpt.com.evil.com/x", ["chatgpt.com"])


def test_empty_allowlist_rejects_everything() -> None:
    with pytest.raises(AiBackupHostDeniedError):
        assert_host_allowed("https://chatgpt.com/x", [])
