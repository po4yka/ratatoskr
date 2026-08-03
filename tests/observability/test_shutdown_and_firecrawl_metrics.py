"""Two places where Ratatoskr could not tell you what happened.

Spans were buffered by BatchSpanProcessor for up to five seconds and nothing ever
called shutdown_tracing. TracerProvider registers its own atexit hook, so a normal
exit was covered -- but SIGTERM is how Docker stops a container, and Python's
default handler terminates without running atexit. Every `docker compose down`
and every deploy therefore dropped the tail of the run: exactly the spans an
operator wants when a process dies mid-request.

Four of the five processes flush now. uvicorn runs the FastAPI lifespan, taskiq
fires WORKER_SHUTDOWN, the bot turns SIGTERM into a cancel of its main task, and
the MCP server hangs the flush off its own ASGI lifespan. The fifth is the taskiq
scheduler: taskiq's scheduler CLI installs no signal handler at all -- unlike its
worker CLI -- so SIGTERM kills that process at SIG_DFL and nothing runs. Giving it
a seam is a separate change.

record_firecrawl_request was called from nowhere. Its counter is labelled, so no
child series was ever created, and a Prometheus aggregate over a missing series
is an empty vector rather than zero. Both alerts built on it could therefore
never fire -- including RatatoskrFirecrawlNoRequests, whose entire job is to
notice that Firecrawl has gone quiet while URLs are still being processed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

ALERTS = Path(__file__).resolve().parents[2] / "ops/monitoring/alerting_rules.yml"


# The CI unit-test environment installs no optional extras, so the OTel SDK is
# absent there. The source-level wiring assertions below still run; only the
# tests that poke the SDK need it.
def _otel_installed() -> bool:
    try:
        return importlib.util.find_spec("opentelemetry") is not None
    except ModuleNotFoundError:
        return False


_needs_otel = pytest.mark.skipif(
    not _otel_installed(),
    reason="opentelemetry SDK is not installed in this environment",
)


class TestTracingShutdown:
    @_needs_otel
    def test_shutdown_flushes_the_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.observability import otel

        provider = MagicMock()
        monkeypatch.setattr(otel, "_otel_available", True)
        monkeypatch.setattr(otel, "_initialized", True)
        monkeypatch.setattr(otel, "_trace", MagicMock(get_tracer_provider=lambda: provider))

        otel.shutdown_tracing()

        provider.shutdown.assert_called_once()

    @_needs_otel
    def test_a_second_call_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """atexit fires after the explicit call; the second must not double-shutdown."""
        from app.observability import otel

        provider = MagicMock()
        monkeypatch.setattr(otel, "_otel_available", True)
        monkeypatch.setattr(otel, "_initialized", True)
        monkeypatch.setattr(otel, "_trace", MagicMock(get_tracer_provider=lambda: provider))

        otel.shutdown_tracing()
        otel.shutdown_tracing()

        assert provider.shutdown.call_count == 1

    @_needs_otel
    def test_it_is_safe_when_tracing_never_started(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.observability import otel

        monkeypatch.setattr(otel, "_otel_available", True)
        monkeypatch.setattr(otel, "_initialized", False)
        otel.shutdown_tracing()  # must not raise

    @_needs_otel
    def test_the_sdk_already_owns_the_atexit_hook(self) -> None:
        """Why this fix lives in the shutdown seams and not in an atexit hook.

        TracerProvider registers atexit.register(self.shutdown) itself, so a
        normal interpreter exit was never the gap. SIGTERM is -- Python's default
        handler terminates without running atexit, and SIGTERM is how Docker stops
        a container. Adding our own atexit hook would have been cargo.
        """
        import inspect

        from opentelemetry.sdk.trace import TracerProvider

        assert (
            inspect.signature(TracerProvider.__init__).parameters["shutdown_on_exit"].default
            is True
        )
        assert "atexit.register(self.shutdown)" in inspect.getsource(TracerProvider.__init__)


class TestTheSigtermPathsFlush:
    """init_tracing is called from five processes; shutdown was called from none.

    Each of these owns a different seam -- the FastAPI lifespan, taskiq's
    WORKER_SHUTDOWN (app/cli/taskiq_worker.py forwards the signal to the child
    process group), a SIGTERM-to-cancel handler, the MCP server's own ASGI
    lifespan -- so there is nothing to share and nothing but this list to notice
    when one of them is dropped. The taskiq scheduler is the one process still
    missing from it; see the module docstring for why it needs its own change.
    """

    @pytest.mark.parametrize(
        "path",
        ["app/api/main.py", "app/tasks/broker.py", "bot.py", "app/mcp/server.py"],
        ids=["mobile-api", "taskiq-worker", "bot", "mcp"],
    )
    def test_the_shutdown_seams_flush_the_span_buffer(self, path: str) -> None:
        source = (Path(__file__).resolve().parents[2] / path).read_text()
        assert "shutdown_tracing" in source, f"{path} never flushes its span buffer"


class TestFirecrawlRequestsAreCounted:
    @pytest.fixture
    def client(self) -> Any:
        from app.adapters.external.firecrawl.client import FirecrawlClient

        return object.__new__(FirecrawlClient)

    @pytest.mark.asyncio
    async def test_a_successful_call_is_recorded(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "app.adapters.external.firecrawl.client.record_firecrawl_request",
            lambda status, endpoint="scrape", latency_seconds=None: calls.append(
                (status, endpoint)
            ),
        )

        async def _post(_url: str, **_kw: Any) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        client._client = MagicMock(post=_post)

        await client._request("POST", "https://fc.test/scrape", endpoint="scrape")

        assert calls == [("success", "scrape")]

    @pytest.mark.asyncio
    async def test_an_http_error_status_is_recorded_as_an_error(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The high-error-rate alert divides by this label."""
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "app.adapters.external.firecrawl.client.record_firecrawl_request",
            lambda status, endpoint="scrape", latency_seconds=None: calls.append(
                (status, endpoint)
            ),
        )

        async def _post(_url: str, **_kw: Any) -> httpx.Response:
            return httpx.Response(502, text="bad gateway")

        client._client = MagicMock(post=_post)

        await client._request("POST", "https://fc.test/scrape", endpoint="scrape")

        assert calls == [("error", "scrape")]

    @pytest.mark.asyncio
    async def test_a_transport_failure_is_recorded_before_it_propagates(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A connect error is exactly when the operator needs the counter to move."""
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "app.adapters.external.firecrawl.client.record_firecrawl_request",
            lambda status, endpoint="scrape", latency_seconds=None: calls.append(
                (status, endpoint)
            ),
        )

        async def _post(_url: str, **_kw: Any) -> httpx.Response:
            raise httpx.ConnectError("refused")

        client._client = MagicMock(post=_post)

        with pytest.raises(httpx.ConnectError):
            await client._request("POST", "https://fc.test/scrape", endpoint="scrape")

        assert calls == [("error", "scrape")]

    def test_no_call_site_bypasses_the_recorder(self) -> None:
        """Every Firecrawl API call must go through _request, or the series has holes.

        The helper itself is the one legitimate user of the bound client methods.
        """
        source = (
            Path(__file__).resolve().parents[2] / "app/adapters/external/firecrawl/client.py"
        ).read_text()
        helper_start = source.index("    async def _request(")
        helper_end = source.index("    async def _execute_scrape_attempt(")
        outside = source[:helper_start] + source[helper_end:]
        for verb in ("post", "get"):
            assert f"self._client.{verb}(" not in outside, (
                f"a raw self._client.{verb}( call skips record_firecrawl_request"
            )


class TestTheAlertsHaveAProducer:
    """The alerts were written against a metric nothing emitted."""

    def test_the_alert_series_is_now_produced(self) -> None:
        alerts = ALERTS.read_text()
        assert "ratatoskr_firecrawl_requests_total" in alerts

        client = (
            Path(__file__).resolve().parents[2] / "app/adapters/external/firecrawl/client.py"
        ).read_text()
        assert "record_firecrawl_request" in client

    def test_the_error_label_the_alert_filters_on_is_emitted(self) -> None:
        """RatatoskrFirecrawlHighErrors filters on status="error"."""
        alerts = ALERTS.read_text()
        assert 'ratatoskr_firecrawl_requests_total{status="error"}' in alerts

        client = (
            Path(__file__).resolve().parents[2] / "app/adapters/external/firecrawl/client.py"
        ).read_text()
        assert '"error"' in client


class TestTheBotCanActuallyReachItsTeardown:
    """Without a SIGTERM handler every shutdown call in bot.py is dead code.

    Compose runs the bot as `exec python -m bot` under tini, which forwards
    SIGTERM. Python leaves SIGTERM at SIG_DFL, so the interpreter dies on the
    spot: the finally block never runs, and neither does OTel's own atexit hook.
    The 30 s stop_grace_period was never consumed.
    """

    @staticmethod
    def _source() -> str:
        return (Path(__file__).resolve().parents[2] / "bot.py").read_text()

    def test_sigterm_is_turned_into_a_cancel(self) -> None:
        source = self._source()
        assert "add_signal_handler" in source
        assert "signal.SIGTERM" in source

    def test_sigint_is_left_to_asyncio(self) -> None:
        """asyncio.Runner already cancels on SIGINT; overriding loses double-Ctrl-C."""
        assert "signal.SIGINT" not in self._source()

    def test_the_handler_is_installed_before_the_bot_starts(self) -> None:
        """A signal arriving during startup must still unwind through the finally."""
        source = self._source()
        assert source.index("add_signal_handler") < source.index("await bot.start()")
