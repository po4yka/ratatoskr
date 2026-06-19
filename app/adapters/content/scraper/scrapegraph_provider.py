"""ScrapeGraph-AI in-process content extraction provider (last-resort LLM-driven)."""

from __future__ import annotations

import asyncio
import importlib
import time
from typing import Any, cast

from pydantic import SecretStr

from app.adapters.content.scraper.target_safety import reject_unsafe_target_url
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SEC = 90

_EXTRACTION_PROMPT = (
    'Return strict JSON with keys {"title": str, "language": str, "body_markdown": str}. '
    "body_markdown must be GitHub-flavored Markdown of the article body, preserving headings, "
    "lists, links, and quotes. Do not include navigation, ads, or comments."
)


class ScrapeGraphAIProvider:
    """Last-resort content extraction via ScrapeGraph-AI (in-process, LLM-driven).

    Uses SmartScraperGraph routed through OpenRouter to extract structured markdown
    from pages that all other providers have failed on. scrapegraphai is a lazy
    optional import so the module loads cleanly without it installed.
    """

    def __init__(
        self,
        openrouter_api_key: str | SecretStr,
        openrouter_model: str,
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
        *,
        min_content_length: int = 400,
    ) -> None:
        # Wrap as SecretStr so the key is never accidentally serialised or logged
        # when the config dict or this object is repr'd/str'd.
        self._openrouter_api_key = (
            openrouter_api_key
            if isinstance(openrouter_api_key, SecretStr)
            else SecretStr(openrouter_api_key)
        )
        self._openrouter_model = openrouter_model
        self._timeout_sec = timeout_sec
        self._min_content_length = min_content_length

    @property
    def provider_name(self) -> str:
        return "scrapegraph_ai"

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        del mobile  # ScrapeGraph-AI does not expose mobile/desktop distinction

        started = time.perf_counter()
        unsafe_result = await reject_unsafe_target_url(
            provider="scrapegraph_ai",
            url=url,
            started=started,
            request_id=request_id,
        )
        if unsafe_result is not None:
            return unsafe_result

        try:
            scrapegraphai = importlib.import_module("scrapegraphai.graphs")
            SmartScraperGraph = scrapegraphai.SmartScraperGraph  # noqa: N806
        except ImportError as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "scrapegraph_ai_import_failed",
                extra={
                    "url": url,
                    "error": str(exc),
                    "hint": "pip install scrapegraphai",
                    "request_id": request_id,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text="ScrapeGraph-AI not installed; run: pip install scrapegraphai",
                latency_ms=latency,
                source_url=url,
                endpoint="scrapegraph_ai",
            )

        # scrapegraphai splits model on "/" to derive model_provider: split[0].
        # Passing model="deepseek/deepseek-v4-flash" would set model_provider="deepseek",
        # which is not in its registry. Prepending "openai/" makes the lib parse
        # model_provider="openai" and passes the remainder (original slash-form string)
        # to OpenRouter, which accepts the full model identifier unchanged.
        # TODO: drop the "openai/" prefix once https://github.com/ScrapeGraphAI/Scrapegraph-ai/issues/560 lands and exposes `override_provider` for OpenAI-compatible base URLs.
        # get_secret_value() is called here — the sole point of use — so the raw key
        # is never resident in a logged/repr'd data structure.
        graph_config: dict[str, Any] = {
            "llm": {
                "api_key": self._openrouter_api_key.get_secret_value(),
                "model": f"openai/{self._openrouter_model}",
                "base_url": "https://openrouter.ai/api/v1",
            },
            "verbose": False,
            "headless": True,
        }

        def _run_graph() -> dict[str, Any]:
            graph = SmartScraperGraph(
                prompt=_EXTRACTION_PROMPT,
                source=url,
                config=graph_config,
            )
            return cast("dict[str, Any]", graph.run())

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_run_graph),
                timeout=self._timeout_sec,
            )
        except TimeoutError:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "scrapegraph_ai_timeout",
                extra={"url": url, "timeout_sec": self._timeout_sec, "request_id": request_id},
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"ScrapeGraph-AI timeout after {self._timeout_sec}s",
                latency_ms=latency,
                source_url=url,
                endpoint="scrapegraph_ai",
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "scrapegraph_ai_run_failed",
                extra={
                    "url": url,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "request_id": request_id,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"ScrapeGraph-AI run failed: {exc}",
                latency_ms=latency,
                source_url=url,
                endpoint="scrapegraph_ai",
            )

        latency = int((time.perf_counter() - started) * 1000)

        if not isinstance(result, dict):
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"ScrapeGraph-AI: unexpected result type {type(result).__name__}",
                latency_ms=latency,
                source_url=url,
                endpoint="scrapegraph_ai",
            )

        body_markdown: str = result.get("body_markdown") or ""

        if not body_markdown or len(body_markdown.strip()) < self._min_content_length:
            logger.info(
                "scrapegraph_ai_thin_content",
                extra={
                    "url": url,
                    "content_len": len(body_markdown.strip()) if body_markdown else 0,
                    "threshold": self._min_content_length,
                    "request_id": request_id,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=(
                    f"ScrapeGraph-AI: content too short "
                    f"({len(body_markdown.strip()) if body_markdown else 0} chars)"
                ),
                latency_ms=latency,
                source_url=url,
                endpoint="scrapegraph_ai",
            )

        return FirecrawlResult(
            status=CallStatus.OK,
            http_status=200,
            content_markdown=body_markdown.strip(),
            metadata_json={
                "title": result.get("title") or "",
                "language": result.get("language") or "",
            },
            latency_ms=latency,
            source_url=url,
            endpoint="scrapegraph_ai",
            options_json={
                "llm_model": self._openrouter_model,
                "provider": "scrapegraph_ai",
            },
        )

    async def aclose(self) -> None:
        pass  # No persistent resources
