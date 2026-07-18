"""Rehydrate bulk graph context from durable request/crawl handles."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


async def load_source_text(state: SummarizeState, deps: SummarizeDeps) -> str:
    """Return transient source text, re-fetching it after checkpoint resume."""
    source_text = (state.get("source_text") or "").strip()
    if source_text:
        return source_text
    request_id = state.get("request_id")
    if request_id is None:
        return ""

    if deps.crawl_repo is not None:
        crawl = await _call_optional(
            getattr(deps.crawl_repo, "async_get_crawl_result_by_request", None), request_id
        )
        if isinstance(crawl, dict):
            for field in ("content_markdown", "content_html"):
                value = crawl.get(field)
                if isinstance(value, str) and value.strip():
                    return value.strip()

    request = await _call_optional(
        getattr(deps.requests, "async_get_request_by_id", None), request_id
    )
    if isinstance(request, dict):
        value = request.get("content_text")
        if isinstance(value, str):
            return value.strip()
    return ""


async def _call_optional(method: object, *args: object) -> object | None:
    """Call an async repository method when the injected port provides it.

    Small graph fixtures intentionally use partial/bare mocks; absence of a read
    method means there is no durable context to rehydrate, not a graph failure.
    """
    if not callable(method):
        return None
    result = method(*args)
    return await result if inspect.isawaitable(result) else None
