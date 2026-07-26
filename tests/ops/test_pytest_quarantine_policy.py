from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from tests import conftest as root_conftest


class _Item:
    nodeid = "tests/test_example.py::test_example"

    def __init__(self, *markers: pytest.MarkDecorator) -> None:
        self._markers = [marker.mark for marker in markers]
        self.added: list[pytest.MarkDecorator] = []

    def iter_markers(self, name: str):
        return (marker for marker in self._markers if marker.name == name)

    def add_marker(self, marker: pytest.MarkDecorator) -> None:
        self.added.append(marker)


def _quarantined(**metadata: Any) -> pytest.MarkDecorator:
    return pytest.mark.quarantined(**metadata)


def test_valid_quarantine_adds_only_bounded_retries() -> None:
    item = _Item(_quarantined(issue="GH-123", owner="platform", expires="2026-07-16"))

    root_conftest._apply_quarantine_policy(item, today=date(2026, 7, 15))  # type: ignore[arg-type]

    assert len(item.added) == 1
    retry = item.added[0].mark
    assert retry.name == "flaky"
    assert retry.args == ()
    assert retry.kwargs == {"reruns": 2, "reruns_delay": 1}


@pytest.mark.parametrize("missing", ["issue", "owner", "expires"])
def test_quarantine_requires_tracking_metadata(missing: str) -> None:
    metadata = {"issue": "GH-123", "owner": "platform", "expires": "2026-07-16"}
    del metadata[missing]
    item = _Item(_quarantined(**metadata))

    with pytest.raises(pytest.UsageError, match=f"missing required metadata: {missing}"):
        root_conftest._apply_quarantine_policy(item, today=date(2026, 7, 15))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (
            {"issue": "", "owner": "platform", "expires": "2026-07-16"},
            "issue must be a non-empty string",
        ),
        (
            {"issue": "GH-123", "owner": "", "expires": "2026-07-16"},
            "owner must be a non-empty string",
        ),
        (
            {"issue": "GH-123", "owner": "platform", "expires": "16-07-2026"},
            "invalid quarantine expiry",
        ),
        (
            {"issue": "GH-123", "owner": "platform", "expires": "2026-07-14"},
            "quarantine expired on 2026-07-14",
        ),
    ],
)
def test_quarantine_rejects_invalid_or_expired_metadata(
    metadata: dict[str, str], message: str
) -> None:
    item = _Item(_quarantined(**metadata))

    with pytest.raises(pytest.UsageError, match=message):
        root_conftest._apply_quarantine_policy(item, today=date(2026, 7, 15))  # type: ignore[arg-type]


def test_direct_flaky_marker_is_rejected() -> None:
    item = _Item(pytest.mark.flaky(reruns=10))

    with pytest.raises(pytest.UsageError, match="flaky is managed"):
        root_conftest._apply_quarantine_policy(item, today=date(2026, 7, 15))  # type: ignore[arg-type]
