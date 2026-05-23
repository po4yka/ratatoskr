"""Tests for the `/x_possible` Telegram command handler."""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.adapters.telegram.command_handlers.x_possible import (
    XPossibleHandler,
)


def _make_cfg(ideas_path: pathlib.Path) -> Any:
    return SimpleNamespace(x_bookmarks=SimpleNamespace(ideas_path=str(ideas_path)))


def _make_ctx(reply: AsyncMock, correlation_id: str = "cid-test") -> Any:
    return SimpleNamespace(
        message=SimpleNamespace(),
        correlation_id=correlation_id,
        response_formatter=SimpleNamespace(safe_reply=reply),
        uid=42,
        user_repo=SimpleNamespace(),
        audit_func=lambda *_a, **_k: None,
    )


def _wrapped(handler: XPossibleHandler) -> Any:
    return handler.handle_x_possible.__wrapped__.__wrapped__


@pytest.mark.asyncio
async def test_replies_with_fallback_when_ideas_dir_missing(tmp_path: pathlib.Path) -> None:
    handler = XPossibleHandler(_make_cfg(tmp_path / "nope"))
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
    handler = XPossibleHandler(_make_cfg(tmp_path / "ideas"))
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

    handler = XPossibleHandler(_make_cfg(ideas), top_n=2)
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

    handler = XPossibleHandler(_make_cfg(ideas))
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

    handler = XPossibleHandler(_make_cfg(ideas))
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

    handler = XPossibleHandler(_make_cfg(ideas))
    reply = AsyncMock()

    await _wrapped(handler)(handler, _make_ctx(reply))

    reply.assert_awaited_once()
    _, text = reply.await_args.args
    assert "no idea nodes" in text


@pytest.mark.asyncio
async def test_extract_nodes_from_ideas_container_key(tmp_path: pathlib.Path) -> None:
    """``_extract_nodes`` must also recognise the ``ideas`` container key."""
    ideas = tmp_path / "ideas"
    ideas.mkdir()
    (ideas / "run.json").write_text(
        json.dumps({"ideas": [{"title": "From ideas key", "prompt": "details"}]}),
        encoding="utf-8",
    )

    handler = XPossibleHandler(_make_cfg(ideas))
    reply = AsyncMock()

    await _wrapped(handler)(handler, _make_ctx(reply))

    _, text = reply.await_args.args
    assert "From ideas key" in text


@pytest.mark.asyncio
async def test_extract_nodes_from_items_container_key(tmp_path: pathlib.Path) -> None:
    """``_extract_nodes`` must also recognise the ``items`` container key."""
    ideas = tmp_path / "ideas"
    ideas.mkdir()
    (ideas / "run.json").write_text(
        json.dumps({"items": [{"title": "From items key", "prompt": "details"}]}),
        encoding="utf-8",
    )

    handler = XPossibleHandler(_make_cfg(ideas))
    reply = AsyncMock()

    await _wrapped(handler)(handler, _make_ctx(reply))

    _, text = reply.await_args.args
    assert "From items key" in text


@pytest.mark.asyncio
async def test_extract_nodes_from_results_container_key(tmp_path: pathlib.Path) -> None:
    """``_extract_nodes`` must also recognise the ``results`` container key."""
    ideas = tmp_path / "ideas"
    ideas.mkdir()
    (ideas / "run.json").write_text(
        json.dumps({"results": [{"title": "From results key", "prompt": "details"}]}),
        encoding="utf-8",
    )

    handler = XPossibleHandler(_make_cfg(ideas))
    reply = AsyncMock()

    await _wrapped(handler)(handler, _make_ctx(reply))

    _, text = reply.await_args.args
    assert "From results key" in text


@pytest.mark.asyncio
async def test_node_body_truncated_at_200_chars(tmp_path: pathlib.Path) -> None:
    """Body strings longer than 200 chars must be truncated with an ellipsis."""
    ideas = tmp_path / "ideas"
    ideas.mkdir()
    long_body = "X" * 250
    (ideas / "run.json").write_text(
        json.dumps({"nodes": [{"title": "Node A", "prompt": long_body}]}),
        encoding="utf-8",
    )

    handler = XPossibleHandler(_make_cfg(ideas))
    reply = AsyncMock()

    await _wrapped(handler)(handler, _make_ctx(reply))

    _, text = reply.await_args.args
    assert "..." in text
    # Exactly 197 Xs + "..."
    assert "X" * 197 + "..." in text
    assert "X" * 198 not in text


@pytest.mark.asyncio
async def test_node_id_field_shown_in_suffix(tmp_path: pathlib.Path) -> None:
    """When a node has ``id``, the suffix ``(id: <value>)`` appears in the reply."""
    ideas = tmp_path / "ideas"
    ideas.mkdir()
    (ideas / "run.json").write_text(
        json.dumps({"nodes": [{"id": "abc-123", "title": "Node with id"}]}),
        encoding="utf-8",
    )

    handler = XPossibleHandler(_make_cfg(ideas))
    reply = AsyncMock()

    await _wrapped(handler)(handler, _make_ctx(reply))

    _, text = reply.await_args.args
    assert "(id: abc-123)" in text


@pytest.mark.asyncio
async def test_node_node_id_field_shown_in_suffix(tmp_path: pathlib.Path) -> None:
    """When a node has ``node_id`` (not ``id``), the suffix still appears."""
    ideas = tmp_path / "ideas"
    ideas.mkdir()
    (ideas / "run.json").write_text(
        json.dumps({"nodes": [{"node_id": "nid-42", "title": "Node with node_id"}]}),
        encoding="utf-8",
    )

    handler = XPossibleHandler(_make_cfg(ideas))
    reply = AsyncMock()

    await _wrapped(handler)(handler, _make_ctx(reply))

    _, text = reply.await_args.args
    assert "(id: nid-42)" in text


@pytest.mark.asyncio
async def test_node_without_known_title_key_uses_fallback(tmp_path: pathlib.Path) -> None:
    """A node with no recognised title key falls back to ``node #<n>``."""
    ideas = tmp_path / "ideas"
    ideas.mkdir()
    (ideas / "run.json").write_text(
        json.dumps({"nodes": [{"unknown_key": "value"}]}),
        encoding="utf-8",
    )

    handler = XPossibleHandler(_make_cfg(ideas))
    reply = AsyncMock()

    await _wrapped(handler)(handler, _make_ctx(reply))

    _, text = reply.await_args.args
    assert "node #1" in text


@pytest.mark.asyncio
async def test_singular_node_count_label(tmp_path: pathlib.Path) -> None:
    """Exactly one node uses the singular label ``(1 node)`` not ``(1 nodes)``."""
    ideas = tmp_path / "ideas"
    ideas.mkdir()
    (ideas / "run.json").write_text(
        json.dumps({"nodes": [{"title": "Solo"}]}),
        encoding="utf-8",
    )

    handler = XPossibleHandler(_make_cfg(ideas))
    reply = AsyncMock()

    await _wrapped(handler)(handler, _make_ctx(reply))

    _, text = reply.await_args.args
    assert "(1 node)" in text
    assert "(1 nodes)" not in text
