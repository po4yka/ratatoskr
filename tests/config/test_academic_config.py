"""Validation tests for AcademicConfig (academic metadata / open-access fallback)."""

from __future__ import annotations

import pytest

from app.config.academic import AcademicConfig

pytestmark = pytest.mark.no_network


def test_defaults_are_off() -> None:
    cfg = AcademicConfig()
    assert cfg.metadata_fallback_enabled is False
    assert cfg.contact_email is None
    assert cfg.api_timeout_sec == 12.0


def test_fallback_enabled_without_email_raises() -> None:
    # ValidationError subclasses ValueError in pydantic v2.
    with pytest.raises(ValueError, match="requires a contact email"):
        AcademicConfig(metadata_fallback_enabled=True)


def test_fallback_enabled_with_email_ok() -> None:
    cfg = AcademicConfig(metadata_fallback_enabled=True, contact_email="me@example.com")
    assert cfg.metadata_fallback_enabled is True
    assert cfg.contact_email == "me@example.com"


def test_blank_email_normalized_to_none() -> None:
    assert AcademicConfig(contact_email="   ").contact_email is None


def test_blank_email_with_fallback_enabled_raises() -> None:
    with pytest.raises(ValueError, match="requires a contact email"):
        AcademicConfig(metadata_fallback_enabled=True, contact_email="   ")


def test_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        AcademicConfig(api_timeout_sec="0")


def test_timeout_parsed_from_string() -> None:
    assert AcademicConfig(api_timeout_sec="7.5").api_timeout_sec == 7.5


@pytest.mark.parametrize(
    "bad_email",
    ["not-an-email", "a b@c.com", "a@b@c.com", "a@c.com\nX-Injected: 1"],
)
def test_malformed_email_raises(bad_email: str) -> None:
    # Rejected at config load -> prevents header/URL injection + typos.
    with pytest.raises(ValueError, match="valid email"):
        AcademicConfig(contact_email=bad_email)


def test_valid_email_accepted() -> None:
    assert AcademicConfig(contact_email="ops@example.com").contact_email == "ops@example.com"
