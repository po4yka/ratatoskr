"""Tests for ScrapeGraphAIProvider."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.adapters.content.scraper.scrapegraph_provider import ScrapeGraphAIProvider

_LONG_BODY = "## Body\n\n" + ("full article text with enough content to pass the threshold. " * 50)


def _make_provider(**kwargs) -> ScrapeGraphAIProvider:
    return ScrapeGraphAIProvider(
        openrouter_api_key="sk-or-test",
        openrouter_model="test/model",
        **kwargs,
    )


def _make_graph_stub(result: dict) -> MagicMock:
    """Return a stub scrapegraphai.graphs module with SmartScraperGraph returning result."""
    graph_instance = MagicMock()
    graph_instance.run.return_value = result

    graph_class = MagicMock(return_value=graph_instance)

    module_stub = MagicMock()
    module_stub.SmartScraperGraph = graph_class
    return module_stub


class TestScrapeGraphAIProvider:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_successful_extraction_returns_ok(self):
        """Graph returns dict with body_markdown -> OK result with correct endpoint."""
        graph_result = {
            "title": "Test Title",
            "language": "en",
            "body_markdown": _LONG_BODY,
        }
        module_stub = _make_graph_stub(graph_result)
        provider = _make_provider(timeout_sec=30)

        with patch(
            "app.adapters.content.scraper.scrapegraph_provider.importlib.import_module",
            return_value=module_stub,
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.endpoint == "scrapegraph_ai"
        assert result.content_markdown is not None
        assert len(result.content_markdown) > 0
        assert result.metadata_json == {"title": "Test Title", "language": "en"}

    @pytest.mark.asyncio(loop_scope="function")
    async def test_missing_body_markdown_returns_error(self):
        """Graph returns dict without body_markdown -> ERROR."""
        graph_result = {"title": "No Body", "language": "en"}
        module_stub = _make_graph_stub(graph_result)
        provider = _make_provider(timeout_sec=30)

        with patch(
            "app.adapters.content.scraper.scrapegraph_provider.importlib.import_module",
            return_value=module_stub,
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "scrapegraph_ai"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_short_body_markdown_returns_error(self):
        """Graph returns dict with body_markdown below min_content_length -> ERROR."""
        graph_result = {"title": "Short", "language": "en", "body_markdown": "tiny"}
        module_stub = _make_graph_stub(graph_result)
        provider = _make_provider(timeout_sec=30, min_content_length=400)

        with patch(
            "app.adapters.content.scraper.scrapegraph_provider.importlib.import_module",
            return_value=module_stub,
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "scrapegraph_ai"
        assert (
            "too short" in (result.error_text or "").lower()
            or "content" in (result.error_text or "").lower()
        )

    @pytest.mark.asyncio(loop_scope="function")
    async def test_import_error_returns_error_with_hint(self):
        """scrapegraphai not installed -> ERROR with install hint."""
        provider = _make_provider(timeout_sec=30)

        with patch(
            "app.adapters.content.scraper.scrapegraph_provider.importlib.import_module",
            side_effect=ImportError("No module named 'scrapegraphai'"),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "scrapegraph_ai"
        assert "scrapegraphai" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_timeout_returns_error(self):
        """asyncio.wait_for timeout -> ERROR result."""
        provider = _make_provider(timeout_sec=1)

        async def _slow_run():
            await asyncio.sleep(10)

        module_stub = MagicMock()
        graph_instance = MagicMock()
        graph_instance.run.side_effect = lambda: (_ for _ in ()).throw(TimeoutError("timeout"))
        module_stub.SmartScraperGraph = MagicMock(return_value=graph_instance)

        with (
            patch(
                "app.adapters.content.scraper.scrapegraph_provider.importlib.import_module",
                return_value=module_stub,
            ),
            patch(
                "asyncio.wait_for",
                side_effect=TimeoutError("scrapegraph timed out"),
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "scrapegraph_ai"
        assert "timeout" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_graph_run_exception_returns_error(self):
        """Exception during graph.run() -> ERROR result."""
        provider = _make_provider(timeout_sec=30)

        graph_instance = MagicMock()
        graph_instance.run.side_effect = RuntimeError("scraping crashed")
        module_stub = MagicMock()
        module_stub.SmartScraperGraph = MagicMock(return_value=graph_instance)

        with patch(
            "app.adapters.content.scraper.scrapegraph_provider.importlib.import_module",
            return_value=module_stub,
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "scrapegraph_ai"
        assert "scraping crashed" in (result.error_text or "")

    @pytest.mark.asyncio(loop_scope="function")
    async def test_model_string_is_prefixed_with_openai_for_scrapegraphai_split_compatibility(
        self,
    ):
        """The config passed to SmartScraperGraph has model prefixed with 'openai/'.

        scrapegraphai splits model on '/' once to derive model_provider: split[0].
        Passing model='deepseek/deepseek-v4-flash' would set model_provider='deepseek',
        which is not in its provider registry. Prefixing with 'openai/' makes the lib
        parse model_provider='openai' and passes the remainder (original slash-form
        string) to OpenRouter, which accepts the full model identifier unchanged.
        """
        provider = ScrapeGraphAIProvider(
            openrouter_api_key="sk-or-test",
            openrouter_model="deepseek/deepseek-v4-flash",
            timeout_sec=30,
        )

        graph_result = {
            "title": "Test",
            "language": "en",
            "body_markdown": _LONG_BODY,
        }
        graph_instance = MagicMock()
        graph_instance.run.return_value = graph_result
        captured_configs: list[dict] = []

        def _capture_graph(prompt: str, source: str, config: dict) -> MagicMock:
            captured_configs.append(config)
            return graph_instance

        graph_class = MagicMock(side_effect=_capture_graph)
        module_stub = MagicMock()
        module_stub.SmartScraperGraph = graph_class

        with patch(
            "app.adapters.content.scraper.scrapegraph_provider.importlib.import_module",
            return_value=module_stub,
        ):
            await provider.scrape_markdown("https://example.com")

        assert len(captured_configs) == 1
        llm_cfg = captured_configs[0]["llm"]
        # Model must be prefixed with 'openai/' so scrapegraphai parses provider correctly
        assert llm_cfg["model"] == "openai/deepseek/deepseek-v4-flash"
        # 'model_provider' must NOT be present; the prefix supplies it via scrapegraphai's split
        assert "model_provider" not in llm_cfg
