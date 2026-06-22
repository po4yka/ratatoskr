"""Tests for notification_type_for_exception (terminal-failure message routing).

A content-fetch failure must surface the accurate ``empty_content`` copy rather
than the misleading ``processing_failed`` LLM-parse copy -- the LLM is never
reached when extraction returns nothing (e.g. an anti-bot 403 on habr.com that
the scraper chain cleans to empty -> "Low-value content detected:
empty_after_cleaning").
"""

from __future__ import annotations

import pytest

from app.application.graphs.summarize.lifecycle import (
    CallBudgetExceeded,
    notification_type_for_exception,
)


class GraphRecursionError(Exception):
    """Stand-in matched by class name (lifecycle stays langgraph-free)."""


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("Low-value content detected: empty_after_cleaning"),
        ValueError("Extraction failed: Direct HTML: no usable content"),
        ValueError("Content text is empty or contains only whitespace"),
    ],
)
def test_extraction_failures_map_to_empty_content(exc: Exception) -> None:
    assert notification_type_for_exception(exc) == "empty_content"


def test_extraction_marker_is_case_insensitive() -> None:
    assert notification_type_for_exception(ValueError("LOW-VALUE CONTENT DETECTED: x")) == (
        "empty_content"
    )


def test_call_budget_exceeded_stays_processing_failed() -> None:
    assert notification_type_for_exception(CallBudgetExceeded("budget")) == "processing_failed"


def test_recursion_error_stays_processing_failed() -> None:
    # GraphRecursionError is matched by name to avoid importing langgraph.
    assert notification_type_for_exception(GraphRecursionError("recursion")) == "processing_failed"


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("summary JSON could not be parsed"),
        RuntimeError("unexpected boom"),
        Exception("generic"),
    ],
)
def test_non_extraction_failures_default_to_processing_failed(exc: Exception) -> None:
    assert notification_type_for_exception(exc) == "processing_failed"
