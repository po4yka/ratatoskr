"""Tests for app.adapters.ai_backup.redaction.redact_urls."""

from __future__ import annotations

from app.adapters.ai_backup.redaction import redact_urls
from app.adapters.ai_backup.repository import AiBackupRepository
from app.db.models.ai_backup import AiAccountBackup, AiBackupService, AiBackupStatus


# ---------------------------------------------------------------------------
# redact_urls unit tests
# ---------------------------------------------------------------------------


def test_url_with_path_and_query_reduced_to_scheme_host() -> None:
    result = redact_urls(
        "Claude session rejected: HTTP 401 on "
        "https://claude.ai/api/organizations/abc/chat_conversations?tree=True"
    )
    assert result == "Claude session rejected: HTTP 401 on https://claude.ai"


def test_multiple_urls_in_one_string() -> None:
    text = (
        "first https://claude.ai/api/orgs/abc?x=1 "
        "second https://api.openai.com/v1/chat/completions?model=gpt-4"
    )
    result = redact_urls(text)
    assert result == "first https://claude.ai second https://api.openai.com"


def test_text_without_url_unchanged() -> None:
    plain = "transient network error: connection refused"
    assert redact_urls(plain) == plain


def test_none_passthrough() -> None:
    assert redact_urls(None) is None


def test_host_preserved_path_and_query_gone() -> None:
    result = redact_urls("https://example.com/some/deep/path?key=secret&other=val")
    assert result == "https://example.com"
    assert "/some" not in (result or "")
    assert "secret" not in (result or "")


# ---------------------------------------------------------------------------
# repository integration: record_failure and mark_auth_expired redact URLs
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, row: object) -> None:
        self._row = row

    async def scalar(self, _stmt: object) -> object:
        return self._row


class _FakeCtx:
    def __init__(self, session: _FakeSession) -> None:
        self._s = session

    async def __aenter__(self) -> _FakeSession:
        return self._s

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _FakeDb:
    def __init__(self, row: object) -> None:
        self._row = row

    def transaction(self) -> _FakeCtx:
        return _FakeCtx(_FakeSession(self._row))


async def test_record_failure_persists_redacted_last_error() -> None:
    from app.adapters.ai_backup.errors import AiBackupErrorCategory

    row = AiAccountBackup(
        user_id=1,
        service=AiBackupService.CLAUDE,
        status=AiBackupStatus.OK,
        consecutive_failures=0,
        total_failures=0,
    )
    repo = AiBackupRepository(_FakeDb(row))
    raw_message = (
        "HTTP 401 on https://claude.ai/api/organizations/abc123/chat_conversations?tree=True"
    )
    await repo.record_failure(
        1,
        AiBackupService.CLAUDE,
        category=AiBackupErrorCategory.AUTH_EXPIRED,
        message=raw_message,
    )
    assert row.last_error is not None
    assert "abc123" not in row.last_error
    assert "chat_conversations" not in row.last_error
    assert "tree=True" not in row.last_error
    assert "https://claude.ai" in row.last_error


async def test_mark_auth_expired_persists_redacted_last_error() -> None:
    row = AiAccountBackup(
        user_id=1,
        service=AiBackupService.CLAUDE,
        status=AiBackupStatus.OK,
    )
    repo = AiBackupRepository(_FakeDb(row))
    raw_message = (
        "Session expired: GET https://claude.ai/api/orgs/xyz/sessions?page=2 returned 401"
    )
    await repo.mark_auth_expired(1, AiBackupService.CLAUDE, raw_message)
    assert row.last_error is not None
    assert "xyz" not in row.last_error
    assert "sessions" not in row.last_error
    assert "page=2" not in row.last_error
    assert "https://claude.ai" in row.last_error
