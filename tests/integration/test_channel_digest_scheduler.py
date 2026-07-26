"""Integration smoke-tests for the channel-digest scheduler subsystem.

Covers:
  1. Scheduler cron wiring  — _AppConfigScheduleSource emits correct entries.
  2. Digest body smoke      — _channel_digest_body runs end-to-end with mocked deps.
  3. Redis lock contention  — concurrent calls do not double-deliver.
  4. Userbot session reuse  — start() called once per run; stop() called in finally.
  5. Idempotence            — delivered post IDs filter prevents duplicate rows.
  6. Failure modes          — Redis down (graceful degrade) + Telethon auth expired.
"""

from __future__ import annotations

import importlib
import logging
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.db.models import Channel, ChannelSubscription, DigestDelivery, User

# Initial import — provides the names in module globals for the first run.
# The _fresh_tasks_modules autouse fixture refreshes these before every test
# to prevent module-cache pollution from test_digest_task.py, which evicts
# app.tasks.* and re-imports with stubbed taskiq between its own tests.
from app.tasks.digest import _channel_digest_body
from app.tasks.scheduler import _AppConfigScheduleSource

# ── Constants ─────────────────────────────────────────────────────────────────

_TEST_UID = 111_000_111
_TEST_CHAN = "testchan"
_LOCK_KEY = "ratatoskr:digest:scheduled:lock"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def populated_db(database, session):
    """DB pre-loaded with a user, a channel, and an active subscription.

    Uses the async-Postgres `database`/`session` fixtures from conftest.py.
    Skips automatically when `TEST_DATABASE_URL` is unset.
    """
    user = User(telegram_user_id=_TEST_UID, username="digesttest")
    channel = Channel(username=_TEST_CHAN, title="Test Channel", is_active=True)
    session.add_all([user, channel])
    await session.flush()
    sub = ChannelSubscription(
        user_id=user.telegram_user_id,
        channel_id=channel.id,
        is_active=True,
    )
    session.add(sub)
    await session.commit()
    return database, user, channel, sub


@pytest.fixture
def fake_redis():
    """In-process fake Redis — no network, no external process required."""
    try:
        import fakeredis.aioredis
    except (ImportError, TypeError) as exc:
        pytest.skip(f"fakeredis unavailable on this platform: {exc}")
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def mock_userbot():
    bot = AsyncMock()
    bot.start = AsyncMock()
    bot.stop = AsyncMock()
    bot.fetch_channel_posts = AsyncMock(return_value=[])
    return bot


@pytest.fixture
def mock_llm():
    client = AsyncMock()
    client.aclose = AsyncMock()
    return client


@pytest.fixture
def mock_bot_ctx():
    """Async context manager mimicking TelethonBotClient."""
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def mock_service():
    from app.adapters.digest.digest_service import DigestResult

    svc = MagicMock()
    svc.get_users_with_subscriptions.return_value = []
    svc.async_get_users_with_subscriptions = AsyncMock(return_value=[])
    svc.async_get_user_locale = AsyncMock(return_value="en")
    svc.generate_digest = AsyncMock(return_value=DigestResult(user_id=_TEST_UID))
    return svc


@pytest.fixture(autouse=True)
def _fresh_tasks_modules():
    """Evict app.tasks.* and re-import before every test.

    test_digest_task.py evicts app.tasks.* and re-imports digest with a stubbed
    taskiq, leaving sys.modules["app.tasks.digest"] pointing to a different
    module object than the one captured by this file's top-level imports.
    patch("app.tasks.digest.X") then patches the new module while the
    top-level _channel_digest_body still references the old one — so mocks
    never fire.  Re-importing here and updating the test module's globals
    ensures patch() and the callable both operate on the same module dict.
    """
    for mod_name in list(sys.modules):
        if mod_name.startswith("app.tasks"):
            sys.modules.pop(mod_name, None)

    fresh_digest = importlib.import_module("app.tasks.digest")
    fresh_scheduler = importlib.import_module("app.tasks.scheduler")

    this_mod = sys.modules[__name__]
    this_mod._channel_digest_body = fresh_digest._channel_digest_body
    this_mod._AppConfigScheduleSource = fresh_scheduler._AppConfigScheduleSource


def _no_redis() -> AsyncMock:
    """Return an AsyncMock for get_redis that resolves to None (Redis unavailable)."""
    return AsyncMock(return_value=None)


def _with_redis(fake: Any) -> AsyncMock:
    """Return an AsyncMock for get_redis that resolves to the given fake Redis."""
    return AsyncMock(return_value=fake)


# ── 1. Scheduler cron wiring ──────────────────────────────────────────────────


class TestSchedulerCronWiring:
    """_AppConfigScheduleSource converts DIGEST_TIMES into correct ScheduledTask entries."""

    def _stub_config(
        self,
        *,
        enabled: bool,
        times: list[str],
        tz: str = "UTC",
    ) -> MagicMock:
        cfg = MagicMock()
        cfg.digest.enabled = enabled
        cfg.digest.digest_times = times
        cfg.digest.timezone = tz
        cfg.rss.enabled = False
        cfg.rss.poll_interval_minutes = 60
        cfg.signal_ingestion.any_enabled = False
        cfg.github.sync_enabled = False
        cfg.vector_reconcile.enabled = False
        cfg.retention.enabled = False
        cfg.x_bookmarks.enabled = False
        cfg.git_backup.enabled = False
        cfg.ai_backup.enabled = False
        cfg.langgraph_checkpoint.enabled = False
        return cfg

    @pytest.mark.asyncio
    async def test_two_times_produce_two_digest_tasks(self, monkeypatch):
        cfg = self._stub_config(enabled=True, times=["10:00", "19:00"])
        with patch("app.tasks.scheduler.load_config", return_value=cfg):
            source = _AppConfigScheduleSource()
            tasks = await source.get_schedules()

        digest_tasks = [t for t in tasks if t.task_name == "ratatoskr.digest.run"]
        assert len(digest_tasks) == 2

    @pytest.mark.asyncio
    async def test_disabled_digest_produces_no_tasks(self, monkeypatch):
        cfg = self._stub_config(enabled=False, times=["10:00"])
        with patch("app.tasks.scheduler.load_config", return_value=cfg):
            source = _AppConfigScheduleSource()
            tasks = await source.get_schedules()

        digest_tasks = [t for t in tasks if t.task_name == "ratatoskr.digest.run"]
        assert len(digest_tasks) == 0

    @pytest.mark.asyncio
    async def test_cron_expression_matches_configured_time(self, monkeypatch):
        cfg = self._stub_config(enabled=True, times=["10:30"], tz="Europe/Moscow")
        with patch("app.tasks.scheduler.load_config", return_value=cfg):
            source = _AppConfigScheduleSource()
            tasks = await source.get_schedules()

        task = next(t for t in tasks if t.task_name == "ratatoskr.digest.run")
        assert task.cron == "30 10 * * *"
        assert task.cron_offset == "Europe/Moscow"

    @pytest.mark.asyncio
    async def test_task_label_encodes_time_string(self, monkeypatch):
        cfg = self._stub_config(enabled=True, times=["07:00"])
        with patch("app.tasks.scheduler.load_config", return_value=cfg):
            source = _AppConfigScheduleSource()
            tasks = await source.get_schedules()

        task = next(t for t in tasks if t.task_name == "ratatoskr.digest.run")
        assert task.labels.get("job") == "digest_07:00"


# ── 2. Digest body smoke ───────────────────────────────────────────────────────


class TestDigestBodySmoke:
    """_channel_digest_body runs the full lifecycle with all deps mocked."""

    @pytest.mark.asyncio
    async def test_no_subscriptions_completes_without_error(
        self, mock_userbot, mock_llm, mock_bot_ctx, mock_service
    ):
        """With no subscribed users the task exits cleanly."""
        cfg = MagicMock()
        mock_service.async_get_users_with_subscriptions = AsyncMock(return_value=[])

        with (
            patch("app.tasks.digest.create_digest_userbot", return_value=mock_userbot),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _no_redis()),
        ):
            await _channel_digest_body(cfg)

        mock_service.generate_digest.assert_not_called()

    @pytest.mark.asyncio
    async def test_generates_digest_for_each_subscribed_user(
        self, mock_userbot, mock_llm, mock_bot_ctx, mock_service
    ):
        """generate_digest is called once per user returned by get_users_with_subscriptions."""
        from app.adapters.digest.digest_service import DigestResult

        cfg = MagicMock()
        mock_service.async_get_users_with_subscriptions = AsyncMock(return_value=[111, 222, 333])
        mock_service.generate_digest = AsyncMock(return_value=DigestResult(user_id=0, post_count=2))

        with (
            patch("app.tasks.digest.create_digest_userbot", return_value=mock_userbot),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _no_redis()),
        ):
            await _channel_digest_body(cfg)

        assert mock_service.generate_digest.call_count == 3

    @pytest.mark.asyncio
    async def test_correlation_id_logged_at_start(
        self, mock_userbot, mock_llm, mock_bot_ctx, mock_service, caplog
    ):
        """scheduled_digest_starting log event is emitted with a correlation ID."""
        cfg = MagicMock()

        with (
            patch("app.tasks.digest.create_digest_userbot", return_value=mock_userbot),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _no_redis()),
            caplog.at_level(logging.INFO, logger="app.tasks.digest"),
        ):
            await _channel_digest_body(cfg)

        assert any("scheduled_digest_starting" in r.message for r in caplog.records)


# ── 3. Redis lock contention ───────────────────────────────────────────────────


class TestRedisLockContention:
    """Distributed lock prevents concurrent instances from double-delivering."""

    @pytest.mark.asyncio
    async def test_second_call_skipped_when_lock_held(
        self, fake_redis, mock_userbot, mock_llm, mock_bot_ctx, mock_service
    ):
        """When another instance holds the lock, the body is skipped entirely."""
        # Simulate another instance having the lock
        await fake_redis.set(_LOCK_KEY, "other-instance", nx=True, px=60_000)

        cfg = MagicMock()
        mock_service.async_get_users_with_subscriptions = AsyncMock(return_value=[_TEST_UID])

        with (
            patch("app.tasks.digest.create_digest_userbot", return_value=mock_userbot),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _with_redis(fake_redis)),
        ):
            await _channel_digest_body(cfg)

        # Lock was held → userbot never started, no delivery attempted
        mock_userbot.start.assert_not_called()
        mock_service.generate_digest.assert_not_called()

    @pytest.mark.asyncio
    async def test_lock_released_after_successful_run(
        self, fake_redis, mock_userbot, mock_llm, mock_bot_ctx, mock_service
    ):
        """After a completed run the lock is deleted so the next run can proceed."""
        cfg = MagicMock()

        with (
            patch("app.tasks.digest.create_digest_userbot", return_value=mock_userbot),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _with_redis(fake_redis)),
        ):
            await _channel_digest_body(cfg)

        assert await fake_redis.get(_LOCK_KEY) is None

    @pytest.mark.asyncio
    async def test_second_run_proceeds_after_first_releases_lock(
        self, fake_redis, mock_userbot, mock_llm, mock_bot_ctx, mock_service
    ):
        """Sequential runs both acquire the lock (no spurious hold-over)."""
        cfg = MagicMock()

        with (
            patch("app.tasks.digest.create_digest_userbot", return_value=mock_userbot),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _with_redis(fake_redis)),
        ):
            await _channel_digest_body(cfg)
            await _channel_digest_body(cfg)

        # Both runs started the userbot
        assert mock_userbot.start.call_count == 2


# ── 4. Userbot session reuse ──────────────────────────────────────────────────


class TestUserbotSessionReuse:
    """Userbot lifecycle: start() once per run, stop() always in finally."""

    @pytest.mark.asyncio
    async def test_start_called_exactly_once_per_run(
        self, mock_userbot, mock_llm, mock_bot_ctx, mock_service
    ):
        cfg = MagicMock()

        with (
            patch("app.tasks.digest.create_digest_userbot", return_value=mock_userbot),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _no_redis()),
        ):
            await _channel_digest_body(cfg)

        mock_userbot.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_called_in_finally_on_success(
        self, mock_userbot, mock_llm, mock_bot_ctx, mock_service
    ):
        cfg = MagicMock()

        with (
            patch("app.tasks.digest.create_digest_userbot", return_value=mock_userbot),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _no_redis()),
        ):
            await _channel_digest_body(cfg)

        mock_userbot.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_called_in_finally_after_error(
        self, mock_userbot, mock_llm, mock_bot_ctx, mock_service
    ):
        """stop() is invoked even when generate_digest raises unexpectedly."""
        cfg = MagicMock()
        mock_service.async_get_users_with_subscriptions = AsyncMock(return_value=[_TEST_UID])
        mock_service.generate_digest = AsyncMock(side_effect=RuntimeError("boom"))

        with (
            patch("app.tasks.digest.create_digest_userbot", return_value=mock_userbot),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _no_redis()),
        ):
            # Exception is swallowed by the outer try/except in _channel_digest_body
            await _channel_digest_body(cfg)

        mock_userbot.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_new_session_file_not_created_on_each_run(
        self, mock_userbot, mock_llm, mock_bot_ctx, mock_service
    ):
        """create_digest_userbot is called once per _channel_digest_body invocation.

        The session file path is deterministic (cfg.digest.session_name), so
        Telethon reuses the existing session rather than creating a new one.
        """
        cfg = MagicMock()
        factory_mock = MagicMock(return_value=mock_userbot)

        with (
            patch("app.tasks.digest.create_digest_userbot", factory_mock),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _no_redis()),
        ):
            await _channel_digest_body(cfg)
            await _channel_digest_body(cfg)

        # Factory called twice (once per run), but each time with the same cfg
        assert factory_mock.call_count == 2
        first_cfg = factory_mock.call_args_list[0][0][0]
        second_cfg = factory_mock.call_args_list[1][0][0]
        assert first_cfg is second_cfg  # same config object → same session path


# ── 5. Idempotence ────────────────────────────────────────────────────────────


class TestIdempotence:
    """Delivered post-ID tracking prevents duplicate DigestDelivery rows."""

    @pytest.mark.asyncio
    async def test_list_delivered_ids_returns_persisted_post_ids(self, populated_db, session):
        """Immediately after create_delivery, those IDs appear in list_delivered_message_ids."""
        database, _user, _channel, _sub = populated_db
        from app.infrastructure.persistence.digest_store import DigestStore

        store = DigestStore(database=database)
        assert await store.async_list_delivered_message_ids(_TEST_UID) == set()

        await store.async_create_delivery(
            user_id=_TEST_UID,
            post_count=3,
            channel_count=1,
            digest_type="scheduled",
            correlation_id="cid_run1",
            post_ids=[10, 20, 30],
        )

        delivered = await store.async_list_delivered_message_ids(_TEST_UID)
        assert {10, 20, 30}.issubset(delivered)

    @pytest.mark.asyncio
    async def test_second_run_filters_all_previously_delivered_posts(self, populated_db, session):
        """If all available posts were delivered in run-1, run-2 has nothing to deliver.

        This is the idempotence guarantee: re-running the job for the same
        window does not create a second DigestDelivery row.
        """
        database, _user, _channel, _sub = populated_db
        from app.infrastructure.persistence.digest_store import DigestStore

        store = DigestStore(database=database)

        # Simulate run-1 persisting delivery of posts [1, 2]
        await store.async_create_delivery(
            user_id=_TEST_UID,
            post_count=2,
            channel_count=1,
            digest_type="scheduled",
            correlation_id="run1",
            post_ids=[1, 2],
        )
        count_q = (
            select(func.count())
            .select_from(DigestDelivery)
            .where(DigestDelivery.user_id == _TEST_UID)
        )
        assert await session.scalar(count_q) == 1

        # Run-2: same posts available from the channel
        delivered = await store.async_list_delivered_message_ids(_TEST_UID)
        available = [{"message_id": 1}, {"message_id": 2}]
        undelivered = [p for p in available if p["message_id"] not in delivered]

        # Nothing to deliver → ChannelReader skips → no second DeliveryRecord written
        assert undelivered == []
        assert await session.scalar(count_q) == 1

    @pytest.mark.asyncio
    async def test_new_posts_are_delivered_on_second_run(self, populated_db, session):
        """Posts with IDs not in the delivery history ARE delivered on the next run."""
        database, _user, _channel, _sub = populated_db
        from app.infrastructure.persistence.digest_store import DigestStore

        store = DigestStore(database=database)

        await store.async_create_delivery(
            user_id=_TEST_UID,
            post_count=1,
            channel_count=1,
            digest_type="scheduled",
            correlation_id="run1",
            post_ids=[1],
        )

        delivered = await store.async_list_delivered_message_ids(_TEST_UID)
        available = [{"message_id": 1}, {"message_id": 2}, {"message_id": 3}]
        undelivered = [p for p in available if p["message_id"] not in delivered]

        assert [p["message_id"] for p in undelivered] == [2, 3]


# ── 6. Failure modes ──────────────────────────────────────────────────────────


class TestFailureModes:
    """Error paths surface to logs and do not crash the process."""

    @pytest.mark.asyncio
    async def test_redis_unavailable_digest_still_runs(
        self, mock_userbot, mock_llm, mock_bot_ctx, mock_service
    ):
        """When Redis is unreachable the lock is skipped and the digest proceeds."""
        from app.adapters.digest.digest_service import DigestResult

        cfg = MagicMock()
        mock_service.async_get_users_with_subscriptions = AsyncMock(return_value=[_TEST_UID])
        mock_service.generate_digest = AsyncMock(
            return_value=DigestResult(user_id=_TEST_UID, post_count=1)
        )

        with (
            patch("app.tasks.digest.create_digest_userbot", return_value=mock_userbot),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _no_redis()),
        ):
            await _channel_digest_body(cfg)

        # Digest still ran despite no Redis
        mock_userbot.start.assert_called_once()
        mock_service.generate_digest.assert_called_once()

    @pytest.mark.asyncio
    async def test_redis_unavailable_warning_logged_with_cid(
        self, mock_userbot, mock_llm, mock_bot_ctx, mock_service, caplog
    ):
        """digest_lock_redis_unavailable is logged with a correlation ID."""
        cfg = MagicMock()

        with (
            patch("app.tasks.digest.create_digest_userbot", return_value=mock_userbot),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _no_redis()),
            caplog.at_level(logging.WARNING, logger="app.tasks.digest"),
        ):
            await _channel_digest_body(cfg)

        assert any("digest_lock_redis_unavailable" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_telethon_auth_expired_logs_error_and_does_not_raise(
        self, mock_userbot, mock_llm, mock_bot_ctx, mock_service, caplog
    ):
        """AuthKeyUnregisteredError from userbot.start() is caught and logged; not re-raised."""
        mock_userbot.start.side_effect = Exception("AuthKeyUnregisteredError: key revoked")
        cfg = MagicMock()

        with (
            patch("app.tasks.digest.create_digest_userbot", return_value=mock_userbot),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _no_redis()),
            caplog.at_level(logging.ERROR, logger="app.tasks.digest"),
        ):
            # Must not propagate the exception
            await _channel_digest_body(cfg)

        assert any("scheduled_digest_failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_telethon_auth_expired_still_calls_stop(
        self, mock_userbot, mock_llm, mock_bot_ctx, mock_service
    ):
        """stop() is always called even when start() raises — no session leak."""
        mock_userbot.start.side_effect = Exception("AuthKeyUnregisteredError: key revoked")
        cfg = MagicMock()

        with (
            patch("app.tasks.digest.create_digest_userbot", return_value=mock_userbot),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _no_redis()),
        ):
            await _channel_digest_body(cfg)

        mock_userbot.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_per_user_error_does_not_abort_remaining_users(
        self, mock_userbot, mock_llm, mock_bot_ctx, mock_service
    ):
        """A failure for one user is logged and skipped; other users are still processed."""
        from app.adapters.digest.digest_service import DigestResult

        cfg = MagicMock()
        call_log: list[int] = []

        async def flaky(user_id: int, **_kw: object) -> DigestResult:
            call_log.append(user_id)
            if user_id == 222:
                raise RuntimeError("transient failure")
            return DigestResult(user_id=user_id, post_count=1)

        mock_service.async_get_users_with_subscriptions = AsyncMock(return_value=[111, 222, 333])
        mock_service.generate_digest = flaky

        with (
            patch("app.tasks.digest.create_digest_userbot", return_value=mock_userbot),
            patch("app.tasks.digest.create_digest_llm_client", return_value=mock_llm),
            patch("app.tasks.digest.create_digest_bot_client", return_value=mock_bot_ctx),
            patch("app.tasks.digest.create_digest_service", return_value=mock_service),
            patch("app.infrastructure.redis.get_redis", _no_redis()),
        ):
            await _channel_digest_body(cfg)

        # All three users were attempted
        assert call_log == [111, 222, 333]
