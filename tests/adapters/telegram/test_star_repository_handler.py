"""Tests for the /star command's argument parsing and result reporting.

The reporting tests matter most: starring is the irreversible half of the command
and filing is the half that quietly does not happen, so a reply that implies both
succeeded when only one did is the failure mode worth pinning.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.telegram.command_handlers.star_repository_handler import _normalize, _render


def _result(**overrides):
    base = {
        "repository_id": 1,
        "full_name": "owner/repo",
        "mode": "star",
        "is_starred": True,
        "lists_applied": [],
        "list_suggestion_source": "none",
        "mirror_id": None,
        "warnings": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("torvalds/linux", "https://github.com/torvalds/linux"),
        ("/torvalds/linux/", "https://github.com/torvalds/linux"),
        ("torvalds/linux extra words", "https://github.com/torvalds/linux"),
        ("https://github.com/torvalds/linux", "https://github.com/torvalds/linux"),
    ],
)
def test_normalize_accepts_shorthand_and_urls(raw, expected):
    assert _normalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "linux",
        "a/b/c",
        "https://gitlab.com/foo/bar",
        "https://example.com",
    ],
)
def test_normalize_rejects_non_repository_input(raw):
    """A non-GitHub host or a malformed path must not be silently coerced.

    The command stars whatever it is handed, so guessing a repository out of an
    ambiguous argument would write to the account on the strength of a guess.
    """
    assert _normalize(raw) is None


def test_render_reports_a_filed_repository_with_its_origin():
    text = _render(_result(lists_applied=["Tools"], list_suggestion_source="knn"))
    assert "Starred: owner/repo" in text
    assert "Filed under: Tools" in text
    assert "matched your existing filing" in text


def test_render_states_plainly_when_nothing_was_filed():
    """Starred-but-unfiled is a partial success and must read as one."""
    text = _render(_result())
    assert "Starred: owner/repo" in text
    assert "Not filed" in text
    assert "Filed under" not in text


def test_render_surfaces_every_warning():
    """Warnings carry the steps that failed after the star already landed."""
    text = _render(
        _result(
            lists_applied=["Tools"],
            list_suggestion_source="llm",
            warnings=["Could not enroll the repository for backup: disk full"],
        )
    )
    assert "Warning: Could not enroll the repository for backup: disk full" in text
    assert "chosen by model" in text


def test_render_mentions_backup_only_when_enrolled():
    assert "Enrolled for backup." in _render(_result(mirror_id=5))
    assert "Enrolled for backup." not in _render(_result(mirror_id=None))
