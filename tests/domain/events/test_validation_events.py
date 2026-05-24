"""Validation coverage for domain event value objects."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.events.request_events import (
    RequestCancelled,
    RequestCompleted,
    RequestCreated,
    RequestFailed,
    RequestStatusChanged,
)
from app.domain.events.rule_events import RuleError, RuleExecuted
from app.domain.events.tag_events import (
    TagAttached,
    TagCreated,
    TagDeleted,
    TagDetached,
    TagMerged,
)
from app.domain.models.request import RequestStatus

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_request_events_accept_valid_values() -> None:
    created = RequestCreated(
        request_id=1,
        user_id=2,
        chat_id=3,
        request_type="url",
        occurred_at=NOW,
    )
    changed = RequestStatusChanged(
        request_id=1,
        old_status=RequestStatus.PENDING,
        new_status=RequestStatus.CRAWLING,
        occurred_at=NOW,
    )
    completed = RequestCompleted(request_id=1, summary_id=10, occurred_at=NOW)
    failed = RequestFailed(
        request_id=1, error_message="boom", error_details={"code": "x"}, occurred_at=NOW
    )
    cancelled = RequestCancelled(request_id=1, cancelled_by_user_id=2, occurred_at=NOW)

    assert created.user_id == 2
    assert changed.new_status is RequestStatus.CRAWLING
    assert completed.summary_id == 10
    assert failed.error_details == {"code": "x"}
    assert cancelled.cancelled_by_user_id == 2


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: RequestCreated(
                request_id=0,
                user_id=1,
                chat_id=1,
                request_type="url",
                occurred_at=NOW,
            ),
            "request_id must be positive",
        ),
        (
            lambda: RequestCreated(
                request_id=1,
                user_id=0,
                chat_id=1,
                request_type="url",
                occurred_at=NOW,
            ),
            "user_id must be positive",
        ),
        (
            lambda: RequestStatusChanged(
                request_id=1,
                old_status=RequestStatus.PENDING,
                new_status=RequestStatus.PENDING,
                occurred_at=NOW,
            ),
            "old_status and new_status must be different",
        ),
        (
            lambda: RequestFailed(request_id=1, error_message="", occurred_at=NOW),
            "error_message cannot be empty",
        ),
        (
            lambda: RequestCancelled(request_id=1, cancelled_by_user_id=0, occurred_at=NOW),
            "cancelled_by_user_id must be positive",
        ),
    ],
)
def test_request_events_reject_invalid_values(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_rule_events_accept_valid_values() -> None:
    executed = RuleExecuted(
        rule_id=1,
        summary_id=None,
        event_type="summary.created",
        matched=True,
        actions_count=2,
        user_id=3,
        occurred_at=NOW,
    )
    error = RuleError(
        rule_id=1,
        event_type="summary.created",
        error_message="bad predicate",
        user_id=3,
        occurred_at=NOW,
    )

    assert executed.actions_count == 2
    assert error.error_message == "bad predicate"


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: RuleExecuted(
                rule_id=0,
                summary_id=None,
                event_type="summary.created",
                matched=False,
                actions_count=0,
                user_id=1,
                occurred_at=NOW,
            ),
            "rule_id must be positive",
        ),
        (
            lambda: RuleError(
                rule_id=1,
                event_type="summary.created",
                error_message="",
                user_id=1,
                occurred_at=NOW,
            ),
            "error_message cannot be empty",
        ),
    ],
)
def test_rule_events_reject_invalid_values(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_tag_events_accept_valid_values() -> None:
    created = TagCreated(tag_id=1, user_id=2, name="python", occurred_at=NOW)
    attached = TagAttached(summary_id=3, tag_id=1, user_id=2, source="manual", occurred_at=NOW)
    detached = TagDetached(summary_id=3, tag_id=1, user_id=2, occurred_at=NOW)
    merged = TagMerged(source_tag_ids=(1, 2), target_tag_id=3, user_id=2, occurred_at=NOW)
    deleted = TagDeleted(tag_id=3, user_id=2, occurred_at=NOW)

    assert created.name == "python"
    assert attached.source == "manual"
    assert detached.summary_id == 3
    assert merged.source_tag_ids == (1, 2)
    assert deleted.tag_id == 3


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: TagCreated(tag_id=0, user_id=1, name="python", occurred_at=NOW),
            "tag_id must be positive",
        ),
        (
            lambda: TagCreated(tag_id=1, user_id=1, name="", occurred_at=NOW),
            "name cannot be empty",
        ),
        (
            lambda: TagAttached(
                summary_id=0, tag_id=1, user_id=1, source="manual", occurred_at=NOW
            ),
            "summary_id must be positive",
        ),
        (
            lambda: TagAttached(summary_id=1, tag_id=1, user_id=1, source="", occurred_at=NOW),
            "source cannot be empty",
        ),
        (
            lambda: TagMerged(source_tag_ids=(), target_tag_id=1, user_id=1, occurred_at=NOW),
            "source_tag_ids cannot be empty",
        ),
        (
            lambda: TagMerged(source_tag_ids=(1, 0), target_tag_id=1, user_id=1, occurred_at=NOW),
            "all source_tag_ids must be positive",
        ),
        (
            lambda: TagMerged(source_tag_ids=(1,), target_tag_id=0, user_id=1, occurred_at=NOW),
            "target_tag_id must be positive",
        ),
        (
            lambda: TagDeleted(tag_id=1, user_id=0, occurred_at=NOW),
            "user_id must be positive",
        ),
    ],
)
def test_tag_events_reject_invalid_values(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()
