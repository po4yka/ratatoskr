"""Tests for the Taskiq dead-letter requeue CLI helpers."""

from __future__ import annotations

from app.cli.requeue_failed_task import _requeue_labels


def test_requeue_labels_reset_retry_counter_but_keep_policy() -> None:
    labels = {
        "retry_on_error": True,
        "max_retries": 3,
        "_retries": 3,
        "correlation_id": "cid-1",
    }

    assert _requeue_labels(labels) == {
        "retry_on_error": True,
        "max_retries": 3,
        "correlation_id": "cid-1",
    }
    assert labels["_retries"] == 3
