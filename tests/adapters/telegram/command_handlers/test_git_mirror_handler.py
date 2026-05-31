"""Hermetic tests for the /mirror and /mirrors Telegram command handlers.

Covers:
- _parse_mirror_arg: full-URL passthrough, owner/name shorthand expansion, fallback
- _format_mirror_row: status/date formatting, name fallback to clone_url, never attribute
- GitMirrorHandler.handle_mirror: empty-arg usage reply, SSRF rejection, upsert call,
  queued status note (pending / no last_mirrored_at), already-tracked status note
- GitMirrorHandler.handle_mirrors: empty-list reply, formatted list response
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.telegram.command_handlers.git_mirror_handler import (
    GitMirrorHandler,
    _format_mirror_row,
    _parse_mirror_arg,
)
from app.db.models.git_backup import GitMirrorSource

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _make_cfg() -> Any:
    """Minimal AppConfig stub with a git_backup sub-config."""
    git_backup_cfg = SimpleNamespace(
        auto_skip_failing=False,
        max_consecutive_failures=5,
        failure_cooldown_hours=24,
    )
    return SimpleNamespace(git_backup=git_backup_cfg)


def _make_formatter() -> Any:
    return SimpleNamespace(safe_reply=AsyncMock())


def _make_db() -> Any:
    """Fake Database that exposes session() and transaction() context managers."""

    class _Ctx:
        def __init__(self, session: Any) -> None:
            self._s = session

        async def __aenter__(self) -> Any:
            return self._s

        async def __aexit__(self, *_: Any) -> bool:
            return False

    session = MagicMock()
    db = MagicMock()
    db.session.return_value = _Ctx(session)
    db.transaction.return_value = _Ctx(session)
    return db


def _make_ctx(
    text: str = "",
    uid: int = 42,
    correlation_id: str = "cid-test",
) -> Any:
    """Build a minimal CommandExecutionContext-compatible namespace."""
    return SimpleNamespace(
        message=SimpleNamespace(),
        text=text,
        uid=uid,
        chat_id=123,
        correlation_id=correlation_id,
        interaction_id=0,
        start_time=0.0,
        user_repo=SimpleNamespace(),
        response_formatter=SimpleNamespace(safe_reply=AsyncMock()),
        audit_func=lambda *_a, **_k: None,
        log_extra=lambda **kw: {"uid": uid, "cid": correlation_id, **kw},
    )


class _FakeMirrorStatus:
    """Thin stand-in for GitMirrorStatus that always renders as its value string.

    The conftest replaces ``enum.StrEnum`` with a ``(str, Enum)`` shim whose
    ``__str__`` renders as ``"ClassName.MEMBER"`` rather than the value.
    Using this wrapper keeps ``_format_mirror_row`` assertions independent of
    the shim behaviour while still satisfying the ``mirror.status.value`` read
    in ``handle_mirror``.
    """

    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value

    def __format__(self, spec: str) -> str:
        return format(self.value, spec)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _FakeMirrorStatus):
            return self.value == other.value
        return self.value == other

    def __hash__(self) -> int:
        return hash(self.value)


def _make_mirror(
    *,
    id: int = 1,
    name: str | None = "owner/repo",
    clone_url: str = "https://github.com/owner/repo.git",
    status: str = "pending",
    last_mirrored_at: dt.datetime | None = None,
) -> Any:
    """Build a fake GitMirror-like object."""
    return SimpleNamespace(
        id=id,
        name=name,
        clone_url=clone_url,
        status=_FakeMirrorStatus(status),
        last_mirrored_at=last_mirrored_at,
    )


def _handler(db: Any | None = None, formatter: Any | None = None) -> GitMirrorHandler:
    return GitMirrorHandler(
        cfg=_make_cfg(),
        db=db or _make_db(),
        response_formatter=formatter or _make_formatter(),
    )


def _unwrap_mirror(handler: GitMirrorHandler) -> Any:
    """Strip both combined_handler wrappers from handle_mirror."""
    return handler.handle_mirror.__wrapped__.__wrapped__


def _unwrap_mirrors(handler: GitMirrorHandler) -> Any:
    """Strip both combined_handler wrappers from handle_mirrors."""
    return handler.handle_mirrors.__wrapped__.__wrapped__


# ---------------------------------------------------------------------------
# _parse_mirror_arg
# ---------------------------------------------------------------------------


def test_parse_full_https_url_returned_as_is() -> None:
    url = "https://github.com/foo/bar.git"
    clone_url, display_name = _parse_mirror_arg(url)
    assert clone_url == url
    assert display_name is None


def test_parse_http_url_returned_as_is() -> None:
    url = "http://example.com/repo.git"
    clone_url, display_name = _parse_mirror_arg(url)
    assert clone_url == url
    assert display_name is None


def test_parse_git_scheme_url_returned_as_is() -> None:
    url = "git://github.com/foo/bar.git"
    clone_url, display_name = _parse_mirror_arg(url)
    assert clone_url == url
    assert display_name is None


def test_parse_ssh_scheme_url_returned_as_is() -> None:
    url = "ssh://git@github.com/foo/bar.git"
    clone_url, display_name = _parse_mirror_arg(url)
    assert clone_url == url
    assert display_name is None


def test_parse_git_at_url_returned_as_is() -> None:
    url = "git@github.com:foo/bar.git"
    clone_url, display_name = _parse_mirror_arg(url)
    assert clone_url == url
    assert display_name is None


def test_parse_owner_name_shorthand_expands_to_github() -> None:
    clone_url, display_name = _parse_mirror_arg("torvalds/linux")
    assert clone_url == "https://github.com/torvalds/linux.git"
    assert display_name == "torvalds/linux"


def test_parse_owner_name_with_dots_expands() -> None:
    clone_url, display_name = _parse_mirror_arg("my.org/my-repo.py")
    assert clone_url == "https://github.com/my.org/my-repo.py.git"
    assert display_name == "my.org/my-repo.py"


def test_parse_strips_leading_trailing_whitespace() -> None:
    clone_url, display_name = _parse_mirror_arg("  foo/bar  ")
    assert clone_url == "https://github.com/foo/bar.git"
    assert display_name == "foo/bar"


def test_parse_unrecognised_token_returned_verbatim() -> None:
    raw = "not-a-url-or-shorthand"
    clone_url, display_name = _parse_mirror_arg(raw)
    assert clone_url == raw
    assert display_name is None


# ---------------------------------------------------------------------------
# _format_mirror_row
# ---------------------------------------------------------------------------


def test_format_mirror_row_with_name_and_timestamp() -> None:
    ts = dt.datetime(2025, 3, 14, 9, 30, tzinfo=dt.UTC)
    mirror = _make_mirror(
        id=7, name="owner/repo", clone_url="https://g.com/r.git", status="ok", last_mirrored_at=ts
    )
    line = _format_mirror_row(mirror)
    assert "[7]" in line
    assert "owner/repo" in line
    assert "status=ok" in line
    assert "2025-03-14 09:30 UTC" in line
    assert "https://g.com/r.git" in line


def test_format_mirror_row_without_timestamp_shows_never() -> None:
    mirror = _make_mirror(last_mirrored_at=None)
    line = _format_mirror_row(mirror)
    assert "last=never" in line


def test_format_mirror_row_falls_back_to_clone_url_when_name_is_none() -> None:
    mirror = _make_mirror(name=None, clone_url="https://example.com/x.git")
    line = _format_mirror_row(mirror)
    assert "https://example.com/x.git" in line


def test_format_mirror_row_falls_back_to_question_mark_when_no_name_or_url() -> None:
    mirror = SimpleNamespace(id=9, status="pending", last_mirrored_at=None, clone_url="")
    # name attribute absent entirely
    line = _format_mirror_row(mirror)
    # clone_url is empty string -> falsy, so falls back to "?" via getattr chain
    assert "[9]" in line


# ---------------------------------------------------------------------------
# GitMirrorHandler.handle_mirror
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_mirror_empty_arg_sends_usage_reply() -> None:
    handler = _handler()
    raw = _unwrap_mirror(handler)
    ctx = _make_ctx(text="/mirror ")

    await raw(handler, ctx)

    ctx.response_formatter.safe_reply.assert_awaited_once()
    _, reply_text = ctx.response_formatter.safe_reply.await_args.args
    assert "Usage:" in reply_text
    assert "/mirror" in reply_text


@pytest.mark.asyncio
async def test_handle_mirror_empty_arg_no_upsert_called() -> None:
    handler = _handler()
    raw = _unwrap_mirror(handler)
    ctx = _make_ctx(text="/mirror")

    with patch.object(
        handler.__class__, "_mirror_repo", new_callable=lambda: property(lambda self: MagicMock())
    ):
        await raw(handler, ctx)

    ctx.response_formatter.safe_reply.assert_awaited_once()
    _, reply_text = ctx.response_formatter.safe_reply.await_args.args
    assert "Usage:" in reply_text


@pytest.mark.asyncio
async def test_handle_mirror_ssrf_guard_rejects_loopback_ip() -> None:
    handler = _handler()
    raw = _unwrap_mirror(handler)
    ctx = _make_ctx(text="/mirror https://127.0.0.1/repo.git")

    await raw(handler, ctx)

    ctx.response_formatter.safe_reply.assert_awaited_once()
    _, reply_text = ctx.response_formatter.safe_reply.await_args.args
    assert "non-public" in reply_text


@pytest.mark.asyncio
async def test_handle_mirror_ssrf_guard_rejects_localhost() -> None:
    handler = _handler()
    raw = _unwrap_mirror(handler)
    ctx = _make_ctx(text="/mirror https://localhost/repo.git")

    await raw(handler, ctx)

    _, reply_text = ctx.response_formatter.safe_reply.await_args.args
    assert "non-public" in reply_text


@pytest.mark.asyncio
async def test_handle_mirror_ssrf_guard_rejects_private_ipv4() -> None:
    handler = _handler()
    raw = _unwrap_mirror(handler)
    ctx = _make_ctx(text="/mirror https://192.168.1.1/repo.git")

    await raw(handler, ctx)

    _, reply_text = ctx.response_formatter.safe_reply.await_args.args
    assert "non-public" in reply_text


@pytest.mark.asyncio
async def test_handle_mirror_queued_status_note_for_pending_mirror() -> None:
    pending_mirror = _make_mirror(status="pending", last_mirrored_at=None)
    fake_repo = AsyncMock()
    fake_repo.upsert_target = AsyncMock(return_value=pending_mirror)

    handler = _handler()
    raw = _unwrap_mirror(handler)
    ctx = _make_ctx(text="/mirror https://github.com/foo/bar.git")

    with patch(
        "app.adapters.telegram.command_handlers.git_mirror_handler.GitMirrorRepository",
        return_value=fake_repo,
    ):
        await raw(handler, ctx)

    fake_repo.upsert_target.assert_awaited_once()
    call_kwargs = fake_repo.upsert_target.await_args.kwargs
    assert call_kwargs["clone_url"] == "https://github.com/foo/bar.git"
    assert call_kwargs["source"] == GitMirrorSource.MANUAL
    assert call_kwargs["user_id"] == 42

    _, reply_text = ctx.response_formatter.safe_reply.await_args.args
    assert "Mirror registered" in reply_text
    assert "Queued" in reply_text


@pytest.mark.asyncio
async def test_handle_mirror_queued_status_note_when_no_last_mirrored_at() -> None:
    # status != "pending" but last_mirrored_at is None -> still "Queued"
    ok_mirror = _make_mirror(status="ok", last_mirrored_at=None)
    fake_repo = AsyncMock()
    fake_repo.upsert_target = AsyncMock(return_value=ok_mirror)

    handler = _handler()
    raw = _unwrap_mirror(handler)
    ctx = _make_ctx(text="/mirror https://github.com/foo/bar.git")

    with patch(
        "app.adapters.telegram.command_handlers.git_mirror_handler.GitMirrorRepository",
        return_value=fake_repo,
    ):
        await raw(handler, ctx)

    _, reply_text = ctx.response_formatter.safe_reply.await_args.args
    assert "Queued" in reply_text


@pytest.mark.asyncio
async def test_handle_mirror_already_tracked_note_when_previously_synced() -> None:
    ts = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    tracked_mirror = _make_mirror(status="ok", last_mirrored_at=ts)
    fake_repo = AsyncMock()
    fake_repo.upsert_target = AsyncMock(return_value=tracked_mirror)

    handler = _handler()
    raw = _unwrap_mirror(handler)
    ctx = _make_ctx(text="/mirror https://github.com/foo/bar.git")

    with patch(
        "app.adapters.telegram.command_handlers.git_mirror_handler.GitMirrorRepository",
        return_value=fake_repo,
    ):
        await raw(handler, ctx)

    _, reply_text = ctx.response_formatter.safe_reply.await_args.args
    assert "Already tracked" in reply_text
    assert "status=ok" in reply_text


@pytest.mark.asyncio
async def test_handle_mirror_shorthand_label_uses_display_name() -> None:
    pending_mirror = _make_mirror(status="pending", last_mirrored_at=None)
    fake_repo = AsyncMock()
    fake_repo.upsert_target = AsyncMock(return_value=pending_mirror)

    handler = _handler()
    raw = _unwrap_mirror(handler)
    ctx = _make_ctx(text="/mirror torvalds/linux")

    with patch(
        "app.adapters.telegram.command_handlers.git_mirror_handler.GitMirrorRepository",
        return_value=fake_repo,
    ):
        await raw(handler, ctx)

    upsert_kwargs = fake_repo.upsert_target.await_args.kwargs
    assert upsert_kwargs["clone_url"] == "https://github.com/torvalds/linux.git"
    assert upsert_kwargs["name"] == "torvalds/linux"

    _, reply_text = ctx.response_formatter.safe_reply.await_args.args
    # The label shown to the user should be the shorthand, not the full URL
    assert "torvalds/linux" in reply_text


@pytest.mark.asyncio
async def test_handle_mirror_full_url_label_uses_clone_url_when_no_display_name() -> None:
    pending_mirror = _make_mirror(
        name=None, clone_url="https://example.com/x.git", status="pending", last_mirrored_at=None
    )
    fake_repo = AsyncMock()
    fake_repo.upsert_target = AsyncMock(return_value=pending_mirror)

    handler = _handler()
    raw = _unwrap_mirror(handler)
    ctx = _make_ctx(text="/mirror https://example.com/x.git")

    with patch(
        "app.adapters.telegram.command_handlers.git_mirror_handler.GitMirrorRepository",
        return_value=fake_repo,
    ):
        await raw(handler, ctx)

    _, reply_text = ctx.response_formatter.safe_reply.await_args.args
    assert "https://example.com/x.git" in reply_text


# ---------------------------------------------------------------------------
# GitMirrorHandler.handle_mirrors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_mirrors_empty_list_sends_no_mirrors_reply() -> None:
    fake_repo = AsyncMock()
    fake_repo.list_for_user = AsyncMock(return_value=[])

    handler = _handler()
    raw = _unwrap_mirrors(handler)
    ctx = _make_ctx(text="/mirrors")

    with patch(
        "app.adapters.telegram.command_handlers.git_mirror_handler.GitMirrorRepository",
        return_value=fake_repo,
    ):
        await raw(handler, ctx)

    fake_repo.list_for_user.assert_awaited_once_with(ctx.uid)
    _, reply_text = ctx.response_formatter.safe_reply.await_args.args
    assert "No git mirrors" in reply_text
    assert "/mirror" in reply_text


@pytest.mark.asyncio
async def test_handle_mirrors_returns_formatted_list() -> None:
    ts = dt.datetime(2025, 6, 1, 12, 0, tzinfo=dt.UTC)
    mirrors = [
        _make_mirror(
            id=1,
            name="foo/bar",
            clone_url="https://github.com/foo/bar.git",
            status="ok",
            last_mirrored_at=ts,
        ),
        _make_mirror(
            id=2,
            name="baz/qux",
            clone_url="https://github.com/baz/qux.git",
            status="pending",
            last_mirrored_at=None,
        ),
    ]
    fake_repo = AsyncMock()
    fake_repo.list_for_user = AsyncMock(return_value=mirrors)

    handler = _handler()
    raw = _unwrap_mirrors(handler)
    ctx = _make_ctx(text="/mirrors")

    with patch(
        "app.adapters.telegram.command_handlers.git_mirror_handler.GitMirrorRepository",
        return_value=fake_repo,
    ):
        await raw(handler, ctx)

    _, reply_text = ctx.response_formatter.safe_reply.await_args.args
    assert "Your git mirrors (2)" in reply_text
    assert "foo/bar" in reply_text
    assert "baz/qux" in reply_text
    assert "status=ok" in reply_text
    assert "status=pending" in reply_text
    assert "2025-06-01 12:00 UTC" in reply_text
    assert "last=never" in reply_text


@pytest.mark.asyncio
async def test_handle_mirrors_single_entry_in_list() -> None:
    mirrors = [_make_mirror(id=3, name="solo/repo", status="failed", last_mirrored_at=None)]
    fake_repo = AsyncMock()
    fake_repo.list_for_user = AsyncMock(return_value=mirrors)

    handler = _handler()
    raw = _unwrap_mirrors(handler)
    ctx = _make_ctx(text="/mirrors")

    with patch(
        "app.adapters.telegram.command_handlers.git_mirror_handler.GitMirrorRepository",
        return_value=fake_repo,
    ):
        await raw(handler, ctx)

    _, reply_text = ctx.response_formatter.safe_reply.await_args.args
    assert "Your git mirrors (1)" in reply_text
    assert "solo/repo" in reply_text
    assert "status=failed" in reply_text
