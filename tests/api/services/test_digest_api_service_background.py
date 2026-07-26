from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.api.services.digest_api_service import DigestAPIService
from app.config.digest import ChannelDigestConfig
from app.db.models import Channel


@pytest.fixture
def digest_service() -> DigestAPIService:
    return DigestAPIService(ChannelDigestConfig(enabled=True))


def test_trigger_channel_digest_normalizes_username(digest_service: DigestAPIService) -> None:
    result = digest_service.trigger_channel_digest(1, "https://t.me/ExampleChan")

    assert result["status"] == "queued"
    assert result["channel"] == "examplechan"
    assert result["correlation_id"]


@pytest.mark.asyncio
async def test_enqueue_methods_schedule_background_work(digest_service: DigestAPIService) -> None:
    digest_service._execute_digest_trigger = AsyncMock()  # type: ignore[method-assign]
    digest_service._execute_channel_digest_trigger = AsyncMock()  # type: ignore[method-assign]

    digest_service.enqueue_digest_trigger(user_id=1, correlation_id="cid-1")
    digest_service.enqueue_channel_digest_trigger(
        user_id=1,
        correlation_id="cid-2",
        channel_username="channel",
    )
    await asyncio.sleep(0)

    digest_service._execute_digest_trigger.assert_awaited_once_with(
        user_id=1,
        correlation_id="cid-1",
    )
    digest_service._execute_channel_digest_trigger.assert_awaited_once_with(
        user_id=1,
        correlation_id="cid-2",
        channel_username="channel",
    )


@pytest.mark.asyncio
async def test_execute_trigger_helpers_swallow_failures(digest_service: DigestAPIService) -> None:
    digest_service._run_digest_task = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(post_count=2, channel_count=1, messages_sent=1, errors=[])
    )

    await digest_service._execute_digest_trigger(user_id=1, correlation_id="cid-1")
    await digest_service._execute_channel_digest_trigger(
        user_id=1,
        correlation_id="cid-2",
        channel_username="chan",
    )

    digest_service._run_digest_task.side_effect = RuntimeError("boom")
    await digest_service._execute_digest_trigger(user_id=1, correlation_id="cid-3")
    await digest_service._execute_channel_digest_trigger(
        user_id=1,
        correlation_id="cid-4",
        channel_username="chan",
    )


@pytest.mark.asyncio
async def test_run_digest_task_executes_digest_service_and_closes_resources(
    digest_service: DigestAPIService,
    db,
    monkeypatch,
) -> None:
    app_cfg = SimpleNamespace(
        openrouter=SimpleNamespace(
            api_key="key",
            model="model",
            fallback_models=["fallback"],
        ),
        telegram=SimpleNamespace(
            api_id=1,
            api_hash="hash",
            bot_token="123:token",
        ),
    )
    monkeypatch.setattr("app.config.load_config", lambda: app_cfg)

    userbot_instance = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    llm_client_instance = SimpleNamespace(aclose=AsyncMock())
    generated_digest = SimpleNamespace(post_count=3, channel_count=1, messages_sent=1, errors=[])
    generated_channel_digest = SimpleNamespace(
        post_count=1,
        channel_count=1,
        messages_sent=1,
        errors=[],
    )
    digest_service_instance = SimpleNamespace(
        generate_digest=AsyncMock(return_value=generated_digest),
        generate_channel_digest=AsyncMock(return_value=generated_channel_digest),
    )

    class FakePyroClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.send_message = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(
        "app.adapters.digest.userbot_client.UserbotClient",
        MagicMock(return_value=userbot_instance),
    )
    monkeypatch.setattr(
        "app.adapters.openrouter.openrouter_client.OpenRouterClient",
        MagicMock(return_value=llm_client_instance),
    )
    monkeypatch.setattr("app.adapters.digest.channel_reader.ChannelReader", MagicMock())
    monkeypatch.setattr("app.adapters.digest.analyzer.DigestAnalyzer", MagicMock())
    monkeypatch.setattr("app.adapters.digest.formatter.DigestFormatter", MagicMock())
    monkeypatch.setattr(
        "app.adapters.digest.digest_service.DigestService",
        MagicMock(return_value=digest_service_instance),
    )
    monkeypatch.setattr(
        "app.adapters.telegram.telethon_compat.TelethonBotClient",
        FakePyroClient,
    )

    result = await digest_service._run_digest_task(
        user_id=1,
        correlation_id="cid-digest",
        channel_username=None,
    )
    channel_result = await digest_service._run_digest_task(
        user_id=1,
        correlation_id="cid-channel",
        channel_username="examplechan",
    )

    assert result is generated_digest
    assert channel_result is generated_channel_digest
    digest_service_instance.generate_digest.assert_awaited_once()
    digest_service_instance.generate_channel_digest.assert_awaited_once()
    async with db.session() as session:
        channel = await session.scalar(select(Channel).where(Channel.username == "examplechan"))
    assert channel is not None
    assert channel.title == "examplechan"
    userbot_instance.start.assert_awaited()
    userbot_instance.stop.assert_awaited()
    llm_client_instance.aclose.assert_awaited()
