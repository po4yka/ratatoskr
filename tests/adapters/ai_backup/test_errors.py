"""Tests for the AI account backup error taxonomy."""

from __future__ import annotations

import json

import pytest

from app.adapters.ai_backup.errors import (
    AiBackupAuthExpiredError,
    AiBackupErrorCategory,
    AiBackupHostDeniedError,
    AiBackupMaxRequestsError,
    AiBackupParseError,
    classify_error,
)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (AiBackupAuthExpiredError("x"), AiBackupErrorCategory.AUTH_EXPIRED),
        (AiBackupHostDeniedError("x"), AiBackupErrorCategory.BLOCKED),
        (AiBackupMaxRequestsError("x"), AiBackupErrorCategory.BLOCKED),
        (AiBackupParseError("x"), AiBackupErrorCategory.PARSE),
        (json.JSONDecodeError("x", "doc", 0), AiBackupErrorCategory.PARSE),
        (TimeoutError(), AiBackupErrorCategory.NETWORK),
        (ConnectionError("x"), AiBackupErrorCategory.NETWORK),
        (OSError("x"), AiBackupErrorCategory.NETWORK),
        (RuntimeError("surprise"), AiBackupErrorCategory.UNKNOWN),
        (ValueError("plain"), AiBackupErrorCategory.UNKNOWN),
    ],
)
def test_classify_error(exc: BaseException, expected: AiBackupErrorCategory) -> None:
    assert classify_error(exc) == expected


def test_classify_error_by_module_name() -> None:
    class _FakeHttpxError(Exception):
        __module__ = "httpx._exceptions"

    class _FakePlaywrightError(Exception):
        __module__ = "playwright._impl"

    assert classify_error(_FakeHttpxError()) == AiBackupErrorCategory.NETWORK
    assert classify_error(_FakePlaywrightError()) == AiBackupErrorCategory.NETWORK
