from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.adapters.external.firecrawl.models import FirecrawlResult
from app.adapters.twitter.firecrawl_extractor import TwitterFirecrawlExtractor
from app.core.call_status import CallStatus


class _FakeFirecrawl:
    def __init__(self, result: FirecrawlResult) -> None:
        self.scrape_markdown = AsyncMock(return_value=result)


class _NullSem:
    def __call__(self) -> _NullSem:
        return self

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> None:
        return None


@pytest.mark.asyncio
async def test_extract_retains_strong_reference_to_persistence_task() -> None:
    """The scheduled crawl-persistence task must be kept alive until completion.

    Regression test: previously the returned asyncio.Task was discarded, so the
    event loop held only a weak reference and the task could be garbage
    collected mid-flight, silently dropping the crawl_results DB write.
    """
    content_markdown = (
        "The city council convened on Tuesday to discuss the proposed budget for "
        "the upcoming fiscal year, with several members raising concerns about "
        "infrastructure spending and public transit funding. Residents attending "
        "the meeting voiced a range of opinions, from support for expanded bus "
        "routes to skepticism about the cost of new bike lanes downtown. The "
        "debate is expected to continue at next month's session, when a final "
        "vote on the budget is scheduled to take place."
    )
    crawl = FirecrawlResult(status=CallStatus.OK, content_markdown=content_markdown)
    release_persist = asyncio.Event()

    async def _slow_persist() -> None:
        await release_persist.wait()

    scheduled_tasks: list[asyncio.Task[None]] = []

    def _schedule(req_id: int, crawl_result: Any, correlation_id: str | None) -> asyncio.Task[None]:
        del req_id, crawl_result, correlation_id
        task = asyncio.create_task(_slow_persist())
        scheduled_tasks.append(task)
        return task

    extractor = TwitterFirecrawlExtractor(
        firecrawl=_FakeFirecrawl(crawl),
        firecrawl_sem=_NullSem(),
        schedule_crawl_persistence=_schedule,
        request_repo=None,
    )

    extract_coro = extractor.extract(
        url_text="https://x.com/user/status/123",
        req_id=1,
        tweet_id="123",
        metadata={},
        correlation_id="cid-1",
        is_article=False,
        persist_result=True,
    )
    extract_task = asyncio.create_task(extract_coro)

    # Give the persistence task a chance to be scheduled and let extract()
    # proceed past the scheduling point without blocking on the slow persist.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert scheduled_tasks, "persistence task was never scheduled"
    persist_task = scheduled_tasks[0]
    assert persist_task in extractor._background_tasks

    release_persist.set()
    await persist_task
    # The done-callback runs synchronously once the task completes; give the
    # event loop one more tick to process it.
    await asyncio.sleep(0)
    assert persist_task not in extractor._background_tasks

    ok, content_text, content_source = await extract_task
    assert ok is True
    assert content_text
    assert content_source == "markdown"
