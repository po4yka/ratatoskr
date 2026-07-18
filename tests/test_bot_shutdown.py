"""Bot shutdown must close external clients and drain tasks."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_bot():
    from app.adapters.telegram.telegram_bot import TelegramBot

    bot = TelegramBot.__new__(TelegramBot)
    bot.cfg = MagicMock()

    # Wire a minimal _runtime.core so _shutdown can reach scraper_chain / llm_client.
    scraper_chain_mock = MagicMock()
    scraper_chain_mock.aclose = AsyncMock()
    llm_client_mock = MagicMock()
    llm_client_mock.aclose = AsyncMock()
    core_mock = MagicMock()
    core_mock.scraper_chain = scraper_chain_mock
    core_mock.llm_client = llm_client_mock
    runtime_mock = MagicMock()
    runtime_mock.core = core_mock
    bot._runtime = runtime_mock

    return bot


@pytest.mark.asyncio
async def test_shutdown_closes_firecrawl_client():
    bot = _make_bot()
    await bot._shutdown()
    bot._runtime.core.scraper_chain.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_closes_llm_client():
    bot = _make_bot()
    await bot._shutdown()
    bot._runtime.core.llm_client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_cleans_openrouter_pool():
    bot = _make_bot()
    with patch(
        "app.adapters.openrouter.openrouter_client.OpenRouterClient.cleanup_all_clients",
        new_callable=AsyncMock,
    ) as mock_cleanup:
        await bot._shutdown()
        mock_cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_tolerates_client_close_failure():
    """Shutdown must not crash if a client's aclose() raises."""
    bot = _make_bot()
    bot._runtime.core.scraper_chain.aclose = AsyncMock(side_effect=RuntimeError("close failed"))
    # Should not raise
    await bot._shutdown()
    # LLM client should still be closed even if scraper chain close fails
    bot._runtime.core.llm_client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_closes_vector_store():
    bot = _make_bot()
    bot.vector_store = MagicMock()
    bot.vector_store.aclose = AsyncMock()
    await bot._shutdown()
    bot.vector_store.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_closes_embedding_service():
    bot = _make_bot()
    bot.embedding_service = MagicMock()
    bot.embedding_service.aclose = AsyncMock()
    await bot._shutdown()
    bot.embedding_service.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_drains_forward_processor():
    """Shutdown must drain the ForwardProcessor's fire-and-forget background tasks.

    Regression guard for the second half of the forward-flow task fix: the
    ForwardProcessor holds strong refs to its insights/related-reads tasks and
    exposes aclose(), but that only matters if _shutdown actually awaits it (as
    it already does for url_processor). Nothing else tests this wiring, so a
    dropped aclose() call in _shutdown would otherwise regress silently.
    """
    bot = _make_bot()
    bot.forward_processor = MagicMock()
    bot.forward_processor.aclose = AsyncMock()

    await bot._shutdown(drain_timeout=3.0)

    bot.forward_processor.aclose.assert_awaited_once_with(timeout=3.0)


@pytest.mark.asyncio
async def test_shutdown_tolerates_forward_processor_close_failure():
    """A failing ForwardProcessor.aclose() must not abort the rest of shutdown."""
    bot = _make_bot()
    bot.forward_processor = MagicMock()
    bot.forward_processor.aclose = AsyncMock(side_effect=RuntimeError("drain failed"))

    # Should not raise, and later steps still run.
    await bot._shutdown()

    bot._runtime.core.llm_client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_drains_audit_tasks():
    """Shutdown should await in-flight audit tasks."""
    completed = False

    async def slow_audit():
        nonlocal completed
        await asyncio.sleep(0.01)
        completed = True

    bot = _make_bot()
    bot._audit_tasks = {asyncio.create_task(slow_audit())}
    await bot._shutdown(drain_timeout=2.0)
    assert completed
