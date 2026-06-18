"""Coverage for the bot's access controller (allow/deny + block reset).

Ported off the legacy DatabaseSessionManager + database_proxy pattern.
Bot construction goes through the async Database fixture from
tests/conftest.py; AccessController-only tests don't construct a bot
at all -- they exercise the controller directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from app.adapters.telegram.access_controller import AccessController
from app.adapters.telegram.telegram_bot import TelegramBot
from tests.conftest import make_test_app_config
from tests.telegram_bot_builders import AUDIT_REPOSITORY_BUILDER, RUNTIME_BUILDER

if TYPE_CHECKING:
    from app.db.session import Database


class FakeMessage:
    def __init__(self, text: str, uid: int) -> None:
        class _User:
            def __init__(self, uid: int) -> None:
                self.id = uid

        class _Chat:
            id = 1

        self.text = text
        self.chat = _Chat()
        self.from_user = _User(uid)
        self._replies: list[str] = []
        self.id = 101
        self.message_id = 101

    async def reply_text(self, text: str, **_kwargs: object) -> None:
        self._replies.append(text)


class FakeTime:
    def __init__(self, start: float = 0.0) -> None:
        self.value = float(start)

    def advance(self, seconds: float) -> None:
        self.value += seconds

    def __call__(self) -> float:
        return self.value


class DummyFormatter:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.error_notifications: list[tuple[str, str]] = []

    async def safe_reply(self, message: object, text: str, **_kwargs: object) -> None:
        self.replies.append(text)

    async def send_error_notification(
        self,
        message: object,
        error_type: str,
        correlation_id: str,
        *,
        details: str | None = None,
    ) -> None:
        self.error_notifications.append((error_type, details or ""))


def _make_config(allowed_ids: list[int]):
    return make_test_app_config(
        db_path="/tmp/access-control-test.db",
        allowed_user_ids=tuple(allowed_ids),
    )


def _make_bot(database: Database, allowed_ids: list[int]) -> TelegramBot:
    cfg = _make_config(allowed_ids)
    from app.adapters import telegram_bot as tbmod

    tbmod.Client = object
    tbmod.filters = None

    with patch("app.adapters.openrouter.openrouter_client.OpenRouterClient") as mock_or:
        mock_or.return_value = AsyncMock()
        return TelegramBot(
            cfg=cfg,
            db=database,
            runtime_builder=RUNTIME_BUILDER,
            audit_repository_builder=AUDIT_REPOSITORY_BUILDER,
        )


# ---------------------------------------------------------------------------
# Bot-level access control: allowed/denied users
# ---------------------------------------------------------------------------


async def test_denied_user_gets_stub(database: Database) -> None:
    bot = _make_bot(database, allowed_ids=[1])
    msg = FakeMessage("/help", uid=999)
    await bot._on_message(msg)
    await bot._shutdown()
    assert any("denied" in r.lower() for r in msg._replies)


async def test_allowed_user_passes(database: Database) -> None:
    bot = _make_bot(database, allowed_ids=[7])
    msg = FakeMessage("/help", uid=7)
    await bot._on_message(msg)
    await bot._shutdown()
    assert any("commands" in r.lower() for r in msg._replies)


# ---------------------------------------------------------------------------
# AccessController-only tests (no bot needed)
# ---------------------------------------------------------------------------


async def test_failed_attempts_reset_after_block_window(database: Database) -> None:
    cfg = _make_config(allowed_ids=[1])
    formatter: Any = DummyFormatter()
    controller = AccessController(cfg, database, formatter, lambda *args, **kwargs: None)
    controller.BLOCK_DURATION_SECONDS = 10

    uid = 999
    message = FakeMessage("/help", uid=uid)
    fake_time = FakeTime()

    with patch("app.adapters.telegram.access_controller.time.time", fake_time):
        for _ in range(controller.MAX_FAILED_ATTEMPTS):
            fake_time.advance(1)
            allowed = await controller.check_access(uid, message, "cid", 0, fake_time())
            assert allowed is False

        # uid is now blocked — still denied within block window
        fake_time.advance(1)
        allowed = await controller.check_access(uid, message, "cid", 0, fake_time())
        assert allowed is False

        # Advance past block window — counter resets, uid gets fresh attempts
        fake_time.advance(controller.BLOCK_DURATION_SECONDS + 1)
        allowed = await controller.check_access(uid, message, "cid", 0, fake_time())
        assert allowed is False

        # Confirm reset: a fresh attempt should not trigger the block notification yet.
        formatter.error_notifications.clear()
        fake_time.advance(1)
        allowed = await controller.check_access(uid, message, "cid", 0, fake_time())
        assert allowed is False
        block_notifs = [e for e, _ in formatter.error_notifications if e == "access_blocked"]
        assert not block_notifs, "uid should not be re-blocked after just 2 fresh attempts"


async def test_stale_tracking_state_is_reclaimed(database: Database) -> None:
    cfg = _make_config(allowed_ids=[1])
    formatter: Any = DummyFormatter()
    controller = AccessController(cfg, database, formatter, lambda *args, **kwargs: None)
    controller.BLOCK_DURATION_SECONDS = 10
    controller.DENY_NOTIFICATION_COOLDOWN_SECONDS = 10

    stale_uid = 999
    fake_time = FakeTime(1)

    with patch("app.adapters.telegram.access_controller.time.time", fake_time):
        # 2 failed attempts at early time (< MAX_FAILED_ATTEMPTS)
        await controller.check_access(stale_uid, FakeMessage("/help", uid=stale_uid), "cid", 0, 0.0)
        fake_time.advance(0.5)
        await controller.check_access(stale_uid, FakeMessage("/help", uid=stale_uid), "cid", 0, 0.0)

    # Advance far past the stale window
    fake_time.value = 20

    with patch("app.adapters.telegram.access_controller.time.time", fake_time):
        # Trigger cleanup via an allowed user call
        allowed = await controller.check_access(1, FakeMessage("/help", uid=1), "cid", 0, 0.0)
        assert allowed is True

        # stale_uid should need MAX fresh attempts to be blocked again
        formatter.error_notifications.clear()
        fake_time.advance(1)
        result = await controller.check_access(
            stale_uid, FakeMessage("/help", uid=stale_uid), "cid", 0, 0.0
        )
        assert result is False  # still denied (not in allowed list)
        block_notifs = [e for e, _ in formatter.error_notifications if e == "access_blocked"]
        assert not block_notifs, (
            "stale_uid should not be blocked on first fresh attempt after cleanup"
        )
