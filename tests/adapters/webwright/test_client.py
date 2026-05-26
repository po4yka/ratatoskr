"""Tests for the Webwright HTTP client adapter."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.adapters.webwright.client import WebwrightClient, WebwrightTaskResult


@pytest.mark.asyncio(loop_scope="function")
async def test_successful_task():
    client = WebwrightClient(url="http://wright:8090", timeout_sec=30)
    sample = {
        "status": "ok",
        "final_answer": "All done.",
        "screenshots": ["/data/x/1.png"],
        "trajectory_path": "/data/x",
        "steps_used": 7,
        "llm_cost_usd": 0.02,
        "error_text": None,
        "latency_ms": 1234,
        "correlation_id": "cid-1",
    }
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=sample))
    with patch(
        "app.adapters.webwright.client.make_safe_async_client",
        return_value=httpx.AsyncClient(transport=transport),
    ):
        result = await client.run_task(task="say hi", correlation_id="cid-1")
    assert isinstance(result, WebwrightTaskResult)
    assert result.status == "ok"
    assert result.final_answer == "All done."
    assert result.screenshots == ("/data/x/1.png",)
    assert result.steps_used == 7
    assert result.llm_cost_usd == 0.02
    assert result.correlation_id == "cid-1"


@pytest.mark.asyncio(loop_scope="function")
async def test_timeout_returns_error_result():
    client = WebwrightClient(url="http://wright:8090", timeout_sec=5)

    def raise_timeout(_req):
        raise httpx.TimeoutException("boom")

    transport = httpx.MockTransport(raise_timeout)
    with patch(
        "app.adapters.webwright.client.make_safe_async_client",
        return_value=httpx.AsyncClient(transport=transport),
    ):
        result = await client.run_task(task="t", correlation_id="c")
    assert result.status == "error"
    assert "timeout" in (result.error_text or "").lower()
    assert result.final_answer is None


@pytest.mark.asyncio(loop_scope="function")
async def test_http_error_returns_error_result():
    client = WebwrightClient(url="http://wright:8090")
    transport = httpx.MockTransport(lambda req: httpx.Response(502, json={}))
    with patch(
        "app.adapters.webwright.client.make_safe_async_client",
        return_value=httpx.AsyncClient(transport=transport),
    ):
        result = await client.run_task(task="t", correlation_id="c")
    assert result.status == "error"
    assert "502" in (result.error_text or "")


@pytest.mark.asyncio(loop_scope="function")
async def test_non_object_payload_returns_error():
    client = WebwrightClient(url="http://wright:8090")
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=["nope"]))
    with patch(
        "app.adapters.webwright.client.make_safe_async_client",
        return_value=httpx.AsyncClient(transport=transport),
    ):
        result = await client.run_task(task="t", correlation_id="c")
    assert result.status == "error"
    assert "non-object" in (result.error_text or "")


@pytest.mark.asyncio(loop_scope="function")
async def test_correlation_id_header_sent():
    client = WebwrightClient(url="http://wright:8090")
    captured: dict[str, str] = {}

    def capture(req):
        for k, v in req.headers.items():
            if k.lower() == "x-correlation-id":
                captured["cid"] = v
        return httpx.Response(
            200,
            json={"status": "ok", "final_answer": "ok", "latency_ms": 1},
        )

    transport = httpx.MockTransport(capture)
    with patch(
        "app.adapters.webwright.client.make_safe_async_client",
        return_value=httpx.AsyncClient(transport=transport),
    ):
        await client.run_task(task="t", correlation_id="abc-123")
    assert captured["cid"] == "abc-123"


@pytest.mark.asyncio(loop_scope="function")
async def test_cookies_and_model_forwarded():
    client = WebwrightClient(url="http://wright:8090", default_model="m-default")
    captured_body: dict = {}

    def capture(req):
        import json as _json

        captured_body.update(_json.loads(req.content))
        return httpx.Response(
            200,
            json={"status": "ok", "final_answer": "ok", "latency_ms": 1},
        )

    transport = httpx.MockTransport(capture)
    with patch(
        "app.adapters.webwright.client.make_safe_async_client",
        return_value=httpx.AsyncClient(transport=transport),
    ):
        await client.run_task(
            task="t",
            correlation_id="c",
            cookies_json={"example.com": [{"name": "sid", "value": "x"}]},
            model="m-override",
            allowed_domains=("example.com",),
        )
    assert captured_body["task"] == "t"
    assert captured_body["model"] == "m-override"
    assert captured_body["allowed_domains"] == ["example.com"]
    assert captured_body["cookies_json"] == {"example.com": [{"name": "sid", "value": "x"}]}
