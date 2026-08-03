"""A broken OpenTelemetry install must not look like OTEL_ENABLED=false.

``init_tracing`` imported the exporter and the three instrumentation packages
unguarded. Those are separate distributions from the SDK and can skew against
it -- reproducible here, where an opentelemetry-instrumentation-httpx newer than
the installed opentelemetry-instrumentation raises ImportError on
``OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST``.

Two of the four callers wrap the call in ``except Exception: pass``
(app/tasks/scheduler.py, app/tasks/broker.py), so the skew produced a process
with no spans, no log line, and nothing to search for. The third, bot.py, does
not guard at all: the same skew would have stopped the bot from booting, over
telemetry.

The two failures deserve different answers. No exporter means no tracing is
possible, so stay off and say so loudly. No auto-instrumentation still leaves
every span Ratatoskr creates itself -- graph nodes, LLM calls, scraper providers
-- so keep the provider and warn.
"""

from __future__ import annotations

import logging
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("opentelemetry", reason="the otel extra is not installed")

from app.observability import otel


@pytest.fixture
def tracing_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh, enabled init_tracing whose SDK objects have no side effects."""
    monkeypatch.setenv("OTEL_ENABLED", "true")
    monkeypatch.setattr(otel, "_initialized", False)
    monkeypatch.setattr(otel, "_otel_available", True)
    monkeypatch.setattr(otel, "_trace", MagicMock())
    monkeypatch.setattr(otel, "TracerProvider", MagicMock())
    # Otherwise this spawns a real exporter thread for the length of the suite.
    monkeypatch.setattr(otel, "BatchSpanProcessor", MagicMock())


def _block(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    """Make ``import module`` raise, the way a version skew does."""
    monkeypatch.setitem(sys.modules, module, None)  # type: ignore[arg-type]


def test_a_broken_exporter_leaves_tracing_off_and_says_so(
    tracing_ready: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def unavailable(_cfg: Any) -> Any:
        raise ImportError("opentelemetry.exporter.otlp.proto.common._exporter_metrics")

    monkeypatch.setattr(otel, "_build_exporter", unavailable)

    with caplog.at_level(logging.ERROR, logger=otel.__name__):
        otel.init_tracing()  # must not raise: bot.py does not guard this call

    assert otel._initialized is False, "a provider with no exporter is worse than none"
    assert "otel_exporter_unavailable" in caplog.text


def test_broken_instrumentation_still_leaves_a_usable_provider(
    tracing_ready: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(otel, "_build_exporter", lambda _cfg: MagicMock())
    _block(monkeypatch, "opentelemetry.instrumentation.httpx")

    with caplog.at_level(logging.WARNING, logger=otel.__name__):
        otel.init_tracing()

    assert otel._initialized is True, "Ratatoskr's own spans do not need the instrumentors"
    assert "otel_library_instrumentation_unavailable" in caplog.text


def test_the_healthy_path_still_instruments(
    tracing_ready: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Guards the guard: a try/except that always fires would pass the test above."""
    instrumented: list[str] = []
    for name in ("httpx", "redis", "logging"):
        module = MagicMock()
        # Each package exposes a differently named instrumentor class.
        for attr in ("HTTPXClientInstrumentor", "RedisInstrumentor", "LoggingInstrumentor"):
            getattr(module, attr).side_effect = lambda _n=name: MagicMock(
                instrument=lambda **_kw: instrumented.append(_n)
            )
        monkeypatch.setitem(sys.modules, f"opentelemetry.instrumentation.{name}", module)
    monkeypatch.setattr(otel, "_build_exporter", lambda _cfg: MagicMock())

    with caplog.at_level(logging.WARNING, logger=otel.__name__):
        otel.init_tracing()

    assert otel._initialized is True
    assert sorted(instrumented) == ["httpx", "logging", "redis"]
    assert "unavailable" not in caplog.text
