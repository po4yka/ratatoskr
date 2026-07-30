from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.adapters.telegram.draft_stream_sender import DraftStreamSender, DraftStreamSettings


class _MessageStub:
    def __init__(self, chat_id: int = 123, message_id: int = 456) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.message_id = message_id


class _BotClientStub:
    """Mirrors the surface of the real TelethonBotClient.

    Deliberately has no ``invoke``: that attribute belongs to the Bot-API-era
    client, and a stub that offers it let ``_send_custom_request`` guard on a
    name the production wrapper never exposes -- which made draft streaming
    raise on every send while the tests stayed green.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def send_custom_request(self, custom_method: str, params: dict[str, object]) -> None:
        self.calls.append((custom_method, params))


@pytest.mark.asyncio
async def test_draft_sender_builds_payload_and_respects_thread_id() -> None:
    telegram_client = SimpleNamespace(client=_BotClientStub())
    sender = DraftStreamSender(
        telegram_client=telegram_client,
        settings=DraftStreamSettings(
            enabled=True, min_interval_ms=0, min_delta_chars=1, max_chars=64
        ),
    )

    with patch.object(sender, "_send_custom_request", AsyncMock()) as send_custom_request:
        message = _MessageStub()

        result = await sender.send_update(
            message,
            "hello draft",
            message_thread_id=777,
            force=True,
        )

        assert result.ok is True
        send_custom_request.assert_awaited_once_with(
            {"chat_id": 123, "text": "hello draft", "message_thread_id": 777}
        )


@pytest.mark.asyncio
async def test_draft_sender_throttles_small_fast_updates() -> None:
    telegram_client = SimpleNamespace(client=_BotClientStub())
    sender = DraftStreamSender(
        telegram_client=telegram_client,
        settings=DraftStreamSettings(
            enabled=True, min_interval_ms=10_000, min_delta_chars=100, max_chars=256
        ),
    )

    with patch.object(sender, "_send_custom_request", AsyncMock()) as send_custom_request:
        message = _MessageStub()

        first = await sender.send_update(message, "first", force=False)
        second = await sender.send_update(message, "first plus", force=False)

        assert first.ok is True
        assert first.sent is True
        assert second.ok is True
        assert second.sent is False
        send_custom_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_draft_sender_cooldown_after_single_failure() -> None:
    """A single failure triggers a 10-second cooldown, not permanent fallback."""
    telegram_client = SimpleNamespace(client=_BotClientStub())
    sender = DraftStreamSender(
        telegram_client=telegram_client,
        settings=DraftStreamSettings(
            enabled=True, min_interval_ms=0, min_delta_chars=1, max_chars=256
        ),
    )

    with patch.object(
        sender,
        "_send_custom_request",
        AsyncMock(side_effect=RuntimeError("unknown method sendMessageDraft")),
    ) as send_custom_request:
        message = _MessageStub()

        first = await sender.send_update(message, "one", force=True)
        second = await sender.send_update(message, "two", force=True)

        assert first.ok is False
        assert first.fallback is True
        # Second call skipped due to cooldown (still reports fallback=True to caller)
        assert second.ok is False
        assert second.fallback is True
        # Only one actual network call was made (second was skipped by cooldown)
        assert send_custom_request.await_count == 1


@pytest.mark.asyncio
async def test_draft_sender_permanent_fallback_after_three_consecutive_failures() -> None:
    """Three consecutive failures trigger permanent fallback for the request."""
    telegram_client = SimpleNamespace(client=_BotClientStub())
    sender = DraftStreamSender(
        telegram_client=telegram_client,
        settings=DraftStreamSettings(
            enabled=True, min_interval_ms=0, min_delta_chars=1, max_chars=256
        ),
    )

    with patch.object(
        sender,
        "_send_custom_request",
        AsyncMock(side_effect=RuntimeError("unknown method sendMessageDraft")),
    ) as send_custom_request:
        message = _MessageStub()
        key = sender.request_key(message)
        assert key is not None

        # Simulate 3 failures with expired cooldowns between them
        for i in range(3):
            state = sender._states.get(key)
            if state is not None:
                state.fallback_until = 0.0  # expire cooldown so next call retries
            await sender.send_update(message, f"attempt-{i}", force=True)

        assert send_custom_request.await_count == 3
        state = sender._states[key]
        assert state.fallback is True
        assert state.consecutive_failures == 3


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Circuit half-opens after reset timeout and closes on a successful probe."""
    import app.adapters.telegram.draft_stream_sender as module

    monkeypatch.setattr(module.time, "time", lambda: 400.0)

    telegram_client = SimpleNamespace(client=_BotClientStub())
    sender = DraftStreamSender(
        telegram_client=telegram_client,
        settings=DraftStreamSettings(
            enabled=True, min_interval_ms=0, min_delta_chars=1, max_chars=256
        ),
    )
    # Pre-set open circuit as if it tripped at t=0 (400s ago)
    sender._transport_disabled = True
    sender._transport_disabled_since = 0.0

    with patch.object(sender, "_send_custom_request", AsyncMock(return_value=None)):
        message = _MessageStub()
        result = await sender.send_update(message, "probe", force=True)

    assert result.ok is True
    assert result.sent is True
    assert sender._transport_disabled is False
    assert sender._transport_consecutive_failures == 0


@pytest.mark.asyncio
async def test_circuit_breaker_reopens_on_half_open_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Circuit re-opens with a fresh timestamp when the half-open probe also fails."""
    import app.adapters.telegram.draft_stream_sender as module

    monkeypatch.setattr(module.time, "time", lambda: 400.0)

    telegram_client = SimpleNamespace(client=_BotClientStub())
    sender = DraftStreamSender(
        telegram_client=telegram_client,
        settings=DraftStreamSettings(
            enabled=True, min_interval_ms=0, min_delta_chars=1, max_chars=256
        ),
    )
    sender._transport_disabled = True
    sender._transport_disabled_since = 0.0

    with patch.object(
        sender,
        "_send_custom_request",
        AsyncMock(side_effect=RuntimeError("telegram_client_invoke_unavailable")),
    ):
        message = _MessageStub()
        result = await sender.send_update(message, "probe", force=True)

    assert result.fallback is True
    assert sender._transport_disabled is True
    assert sender._transport_disabled_since == 400.0


@pytest.mark.asyncio
async def test_send_update_reaches_the_real_transport() -> None:
    """Exercise _send_custom_request itself, not a patched stand-in.

    Every other test in this file patches _send_custom_request away, which is
    why a guard requiring an attribute the production wrapper never exposes
    could break draft streaming completely without turning the suite red.
    """
    bot_client = _BotClientStub()
    sender = DraftStreamSender(
        telegram_client=SimpleNamespace(client=bot_client),
        settings=DraftStreamSettings(
            enabled=True, min_interval_ms=0, min_delta_chars=1, max_chars=64
        ),
    )

    result = await sender.send_update(_MessageStub(), "live draft", force=True)

    assert result.ok is True
    assert result.sent is True
    assert result.fallback is False
    assert bot_client.calls == [
        ("sendMessageDraft", {"chat_id": 123, "text": "live draft"}),
    ]


@pytest.mark.asyncio
async def test_send_update_falls_back_when_transport_is_absent() -> None:
    """A client without send_custom_request degrades to fallback, not a crash."""
    sender = DraftStreamSender(
        telegram_client=SimpleNamespace(client=SimpleNamespace()),
        settings=DraftStreamSettings(
            enabled=True, min_interval_ms=0, min_delta_chars=1, max_chars=64
        ),
    )

    result = await sender.send_update(_MessageStub(), "no transport", force=True)

    assert result.ok is False
    assert result.fallback is True
