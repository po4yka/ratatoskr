"""Tests for app.adapters.telegram.worker_telegram_sender.WorkerTelegramSender."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.adapters.telegram.worker_telegram_sender import (
    WorkerTelegramSender,
    _extract_retry_after,
    _truncate,
)

# ── _truncate ─────────────────────────────────────────────────────────────────


class TestTruncate:
    def test_short_text_unchanged(self) -> None:
        assert _truncate("hello") == "hello"

    def test_exactly_4096_chars_unchanged(self) -> None:
        text = "x" * 4096
        assert _truncate(text) == text

    def test_text_over_limit_truncated(self) -> None:
        text = "a" * 5000
        result = _truncate(text)
        assert len(result) == 4096
        assert result.endswith("\n[truncated]")

    def test_truncation_marker_appended(self) -> None:
        text = "z" * 4100
        result = _truncate(text)
        assert "\n[truncated]" in result


# ── _extract_retry_after ──────────────────────────────────────────────────────


class TestExtractRetryAfter:
    def _make_response(self, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        response = MagicMock(spec=httpx.Response)
        response.json.return_value = body
        response.headers = headers or {}
        return response

    def test_reads_parameters_retry_after(self) -> None:
        response = self._make_response({"parameters": {"retry_after": 42}})
        assert _extract_retry_after(response) == 42.0

    def test_minimum_one_second(self) -> None:
        response = self._make_response({"parameters": {"retry_after": 0}})
        assert _extract_retry_after(response) == 1.0

    def test_falls_back_to_retry_after_header(self) -> None:
        response = self._make_response({}, headers={"Retry-After": "30"})
        assert _extract_retry_after(response) == 30.0

    def test_default_when_no_retry_info(self) -> None:
        response = self._make_response({})
        assert _extract_retry_after(response) == 5.0

    def test_invalid_json_falls_back_to_default(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.json.side_effect = ValueError("bad json")
        response.headers = {}
        assert _extract_retry_after(response) == 5.0


# ── WorkerTelegramSender ──────────────────────────────────────────────────────


def _make_ok_response(result: dict[str, Any]) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.is_success = True
    response.json.return_value = {"ok": True, "result": result}
    return response


def _make_error_response(status_code: int) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.is_success = False
    response.text = "error"
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "error", request=MagicMock(), response=MagicMock()
    )
    return response


def _make_rate_limited_response(retry_after: float = 2.0) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 429
    response.is_success = False
    response.json.return_value = {"parameters": {"retry_after": retry_after}}
    response.headers = {}
    return response


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_returns_message_id_on_success(self) -> None:
        sender = WorkerTelegramSender(bot_token="123:tok")
        ok_response = _make_ok_response({"message_id": 42})

        with patch.object(sender._client, "post", new=AsyncMock(return_value=ok_response)):
            message_id = await sender.send_message(chat_id=100, text="hello")

        assert message_id == 42

    @pytest.mark.asyncio
    async def test_includes_reply_to_when_given(self) -> None:
        sender = WorkerTelegramSender(bot_token="123:tok")
        ok_response = _make_ok_response({"message_id": 7})
        post_mock = AsyncMock(return_value=ok_response)

        with patch.object(sender._client, "post", new=post_mock):
            await sender.send_message(chat_id=100, text="hi", reply_to=5)

        call_kwargs = post_mock.call_args[1]
        assert call_kwargs["json"]["reply_to_message_id"] == 5

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self) -> None:
        sender = WorkerTelegramSender(bot_token="123:tok")
        error_response = _make_error_response(500)

        with patch.object(sender._client, "post", new=AsyncMock(return_value=error_response)):
            with pytest.raises(httpx.HTTPStatusError):
                await sender.send_message(chat_id=100, text="fail")

    @pytest.mark.asyncio
    async def test_truncates_long_text(self) -> None:
        sender = WorkerTelegramSender(bot_token="123:tok")
        ok_response = _make_ok_response({"message_id": 1})
        post_mock = AsyncMock(return_value=ok_response)
        long_text = "x" * 5000

        with patch.object(sender._client, "post", new=post_mock):
            await sender.send_message(chat_id=1, text=long_text)

        sent_text = post_mock.call_args[1]["json"]["text"]
        assert len(sent_text) == 4096

    @pytest.mark.asyncio
    async def test_retries_on_429(self) -> None:
        sender = WorkerTelegramSender(bot_token="123:tok")
        rate_limited = _make_rate_limited_response(0.001)
        ok_response = _make_ok_response({"message_id": 3})
        post_mock = AsyncMock(side_effect=[rate_limited, ok_response])

        with patch.object(sender._client, "post", new=post_mock):
            message_id = await sender.send_message(chat_id=1, text="hi")

        assert message_id == 3
        assert post_mock.call_count == 2


class TestEditMessageText:
    @pytest.mark.asyncio
    async def test_succeeds_on_ok_response(self) -> None:
        sender = WorkerTelegramSender(bot_token="123:tok")
        ok_response = _make_ok_response({"message_id": 9})

        with patch.object(sender._client, "post", new=AsyncMock(return_value=ok_response)):
            await sender.edit_message_text(chat_id=1, message_id=9, text="updated")

    @pytest.mark.asyncio
    async def test_raises_on_4xx_error(self) -> None:
        sender = WorkerTelegramSender(bot_token="123:tok")
        error_response = _make_error_response(400)

        with patch.object(sender._client, "post", new=AsyncMock(return_value=error_response)):
            with pytest.raises(httpx.HTTPStatusError):
                await sender.edit_message_text(chat_id=1, message_id=9, text="fail")


class TestAclose:
    @pytest.mark.asyncio
    async def test_closes_underlying_client(self) -> None:
        sender = WorkerTelegramSender(bot_token="123:tok")
        close_mock = AsyncMock()
        with patch.object(sender._client, "aclose", new=close_mock):
            await sender.aclose()
        close_mock.assert_called_once()
