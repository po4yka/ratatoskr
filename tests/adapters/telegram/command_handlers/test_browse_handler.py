"""Hermetic tests for the /browse Telegram command handler's host-allowlist gate.

Covers:
- Empty WEBWRIGHT_HOST_ALLOWLIST -> handler refuses without calling the sidecar.
- Non-empty allowlist -> handler forwards the configured domains to run_task.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.adapters.telegram.command_handlers.browse_handler import BrowseHandler


def _make_db() -> Any:
    class _Ctx:
        def __init__(self, session: Any) -> None:
            self._s = session

        async def __aenter__(self) -> Any:
            return self._s

        async def __aexit__(self, *_: Any) -> bool:
            return False

    added: list[Any] = []

    def _add(row: Any) -> None:
        added.append(row)

    async def _flush() -> None:
        for row in added:
            if getattr(row, "id", None) is None:
                row.id = 1

    session = SimpleNamespace(
        add=_add,
        flush=_flush,
        commit=AsyncMock(),
        execute=AsyncMock(),
    )
    return SimpleNamespace(session=lambda: _Ctx(session))


def _make_ctx(text: str = "/browse do a thing") -> Any:
    return SimpleNamespace(
        message=SimpleNamespace(),
        text=text,
        uid=42,
        chat_id=123,
        correlation_id="cid-test",
        interaction_id=0,
        start_time=0.0,
        user_repo=SimpleNamespace(),
        response_formatter=SimpleNamespace(safe_reply=AsyncMock()),
        audit_func=lambda *_a, **_k: None,
    )


@pytest.mark.asyncio
async def test_empty_allowlist_refuses_without_calling_sidecar() -> None:
    webwright_client = SimpleNamespace(run_task=AsyncMock())
    handler = BrowseHandler(
        db=_make_db(),
        response_formatter=SimpleNamespace(safe_reply=AsyncMock()),
        webwright_client=cast("Any", webwright_client),
        host_allowlist=(),
    )
    ctx = _make_ctx()

    response_type, _ = await handler.handle_browse(ctx)

    assert response_type == "browse_refused_empty_allowlist"
    webwright_client.run_task.assert_not_awaited()
    reply_text = ctx.response_formatter.safe_reply.call_args.args[1]
    assert "WEBWRIGHT_HOST_ALLOWLIST" in reply_text
    assert "cid-test" in reply_text


@pytest.mark.asyncio
async def test_non_empty_allowlist_is_forwarded_to_run_task() -> None:
    from app.adapters.webwright.client import WebwrightTaskResult

    webwright_client = SimpleNamespace(
        run_task=AsyncMock(
            return_value=WebwrightTaskResult(
                status="ok",
                final_answer="done",
                screenshots=(),
                trajectory_path=None,
                steps_used=3,
                llm_cost_usd=0.01,
                error_text=None,
                latency_ms=100,
                correlation_id="cid-test",
            )
        )
    )
    handler = BrowseHandler(
        db=_make_db(),
        response_formatter=SimpleNamespace(safe_reply=AsyncMock()),
        webwright_client=cast("Any", webwright_client),
        host_allowlist=("example.com", "news.ycombinator.com"),
    )
    ctx = _make_ctx()

    response_type, _ = await handler.handle_browse(ctx)

    assert response_type == "browse_completed"
    webwright_client.run_task.assert_awaited_once()
    call_kwargs = webwright_client.run_task.call_args.kwargs
    assert call_kwargs["allowed_domains"] == ("example.com", "news.ycombinator.com")
