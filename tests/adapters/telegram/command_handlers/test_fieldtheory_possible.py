"""Tests for the `/fieldtheory_possible` Telegram command handler."""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.adapters.telegram.command_handlers.fieldtheory_possible import (
    FieldTheoryPossibleHandler,
)


def _make_cfg(ideas_path: pathlib.Path) -> Any:
    return SimpleNamespace(fieldtheory=SimpleNamespace(ideas_path=str(ideas_path)))


def _make_ctx(reply: AsyncMock, correlation_id: str = "cid-test") -> Any:
    return SimpleNamespace(
        message=SimpleNamespace(),
        correlation_id=correlation_id,
        response_formatter=SimpleNamespace(safe_reply=reply),
        uid=42,
        user_repo=SimpleNamespace(),
        audit_func=lambda *_a, **_k: None,
    )


def _wrapped(handler: FieldTheoryPossibleHandler) -> Any:
    return handler.handle_fieldtheory_possible.__wrapped__.__wrapped__


@pytest.mark.asyncio
async def test_replies_with_fallback_when_ideas_dir_missing(tmp_path: pathlib.Path) -> None:
    handler = FieldTheoryPossibleHandler(_make_cfg(tmp_path / "nope"))
    reply = AsyncMock()
    ctx = _make_ctx(reply)

    await _wrapped(handler)(handler, ctx)

    reply.assert_awaited_once()
    _, text = reply.await_args.args
    assert "No ideas yet" in text
    assert "ft possible run" in text


@pytest.mark.asyncio
async def test_replies_with_fallback_when_no_json_files(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ideas").mkdir()
    handler = FieldTheoryPossibleHandler(_make_cfg(tmp_path / "ideas"))
    reply = AsyncMock()

    await _wrapped(handler)(handler, _make_ctx(reply))

    reply.assert_awaited_once()
    _, text = reply.await_args.args
    assert "No ideas yet" in text


@pytest.mark.asyncio
async def test_renders_top_nodes_from_newest_file(tmp_path: pathlib.Path) -> None:
    ideas = tmp_path / "ideas"
    ideas.mkdir()
    older = ideas / "20260101.json"
    older.write_text(
        json.dumps({"nodes": [{"title": "OLD", "prompt": "should not appear"}]}),
        encoding="utf-8",
    )
    newer = ideas / "20260523.json"
    newer.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "n1", "title": "Build foo", "prompt": "Explore foo angle."},
                    {"id": "n2", "title": "Build bar", "prompt": "Explore bar angle."},
                    {"id": "n3", "title": "Build baz", "prompt": "Explore baz angle."},
                ]
            }
        ),
        encoding="utf-8",
    )
    import os as _os

    _os.utime(older, (1000, 1000))
    _os.utime(newer, (2000, 2000))

    handler = FieldTheoryPossibleHandler(_make_cfg(ideas), top_n=2)
    reply = AsyncMock()

    await _wrapped(handler)(handler, _make_ctx(reply))

    reply.assert_awaited_once()
    _, text = reply.await_args.args
    assert "20260523.json" in text
    assert "Build foo" in text
    assert "Build bar" in text
    assert "Build baz" not in text  # respects top_n
    assert "OLD" not in text  # picked newest, not older
    assert "(3 nodes)" in text


@pytest.mark.asyncio
async def test_reports_error_id_on_parse_failure(tmp_path: pathlib.Path) -> None:
    ideas = tmp_path / "ideas"
    ideas.mkdir()
    (ideas / "broken.json").write_text("{not-json", encoding="utf-8")

    handler = FieldTheoryPossibleHandler(_make_cfg(ideas))
    reply = AsyncMock()

    await _wrapped(handler)(handler, _make_ctx(reply, correlation_id="cid-parse-fail"))

    reply.assert_awaited_once()
    _, text = reply.await_args.args
    assert "Could not read ideas file" in text
    assert "cid-parse-fail" in text


@pytest.mark.asyncio
async def test_accepts_bare_list_payload(tmp_path: pathlib.Path) -> None:
    ideas = tmp_path / "ideas"
    ideas.mkdir()
    (ideas / "flat.json").write_text(
        json.dumps([{"title": "Inline node", "description": "Body."}]),
        encoding="utf-8",
    )

    handler = FieldTheoryPossibleHandler(_make_cfg(ideas))
    reply = AsyncMock()

    await _wrapped(handler)(handler, _make_ctx(reply))

    reply.assert_awaited_once()
    _, text = reply.await_args.args
    assert "Inline node" in text
    assert "Body." in text


@pytest.mark.asyncio
async def test_reports_empty_payload_when_no_nodes(tmp_path: pathlib.Path) -> None:
    ideas = tmp_path / "ideas"
    ideas.mkdir()
    (ideas / "empty.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")

    handler = FieldTheoryPossibleHandler(_make_cfg(ideas))
    reply = AsyncMock()

    await _wrapped(handler)(handler, _make_ctx(reply))

    reply.assert_awaited_once()
    _, text = reply.await_args.args
    assert "no idea nodes" in text
