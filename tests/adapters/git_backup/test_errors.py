"""Characterization tests for error classification (port of ErrorCategoryTest.kt).

Port of ``tests/test_error_category.py`` from the gitout standalone CLI.
JSON fixtures are co-located at ``tests/adapters/git_backup/fixtures/error_category/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.adapters.git_backup import errors
from app.adapters.git_backup.errors import ErrorCategory

_FIXTURES = Path(__file__).parent / "fixtures"


def _load_json(*parts: str) -> Any:
    """Load a JSON fixture relative to this test's fixtures directory."""
    return json.loads(_FIXTURES.joinpath(*parts).read_text())


_CLASSIFY = _load_json("error_category", "classify_cases.json")["cases"]
_PROPERTIES = _load_json("error_category", "properties.json")["categories"]


@pytest.mark.characterization
@pytest.mark.parametrize(
    "case",
    _CLASSIFY,
    ids=[c.get("note") or repr(c["message"])[:40] for c in _CLASSIFY],
)
def test_classify(case: dict) -> None:
    assert errors.classify(case["message"]) is ErrorCategory[case["expected"]]


@pytest.mark.characterization
@pytest.mark.parametrize("name", list(_PROPERTIES.keys()))
def test_category_properties(name: str) -> None:
    category = ErrorCategory[name]
    expected = _PROPERTIES[name]
    assert errors.is_retryable(category) is expected["is_retryable"]
    assert errors.should_use_http1_fallback(category) is expected["should_use_http1_fallback"]
    assert errors.delay_multiplier(category) == expected["delay_multiplier"]
    assert errors.display_name(category) == expected["display_name"]
    assert errors.suggestion(category) == expected["suggestion"]


class TestDiskFullBeatsFetchPack:
    """A full SD card used to look like a network blip.

    git reports ENOSPC during index-pack as "fatal: fetch-pack: invalid
    index-pack output". "fetch-pack" sits in the NETWORK_ERROR needle list, which
    is checked before the storage block, so a doomed clone was classified
    retryable: it burned every retry, walked the repo into consecutive_failures
    and backoff_until, and never tripped the storage circuit breaker that exists
    for exactly this condition.
    """

    def test_enospc_wrapped_in_a_fetch_pack_message_is_storage(self) -> None:
        message = (
            "fatal: write error: No space left on device\n"
            "fatal: fetch-pack: invalid index-pack output"
        )
        assert errors.classify(message) is ErrorCategory.STORAGE_ERROR

    def test_a_disk_full_clone_is_not_retried(self) -> None:
        assert errors.is_retryable(ErrorCategory.STORAGE_ERROR) is False

    def test_disk_quota_also_wins(self) -> None:
        message = "fatal: disk quota exceeded\nfatal: fetch-pack: invalid index-pack output"
        assert errors.classify(message) is ErrorCategory.STORAGE_ERROR

    def test_a_genuine_fetch_pack_failure_is_still_a_network_error(self) -> None:
        """The reorder must not swallow the case the needle was added for."""
        assert (
            errors.classify("error: fetch-pack: invalid index-pack output")
            is ErrorCategory.NETWORK_ERROR
        )
